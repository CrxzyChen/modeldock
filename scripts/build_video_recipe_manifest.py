"""Read fixed HF metadata and small Git blobs; print manifests, never fetch weights.

Release preparation only. No writes, credentials, model imports or production access.
Large/LFS file SHA-256 values come from the pinned repository's LFS metadata.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import fnmatch
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import urllib.parse
import urllib.request
import urllib.error


PINS = {
    "wan2.1-t2v-1.3b": ("Wan-AI/Wan2.1-T2V-1.3B-Diffusers", "0fad780a534b6463e45facd96134c9f345acfa5b"),
    "minimax-h3-ref2va": ("MiniMaxAI/MiniMax-H3", "42ed227ee7df40d41602854ae760620d6eb651fe"),
    "ltx-2.3-distilled": ("diffusers/LTX-2.3-Diffusers", "8eee8edcf067e838b843f926ec4d4cc9b2be1aaf"),
    "wan2.2-i2v-a14b": ("Wan-AI/Wan2.2-I2V-A14B-Diffusers", "596658fd9ca6b7b71d5057529bbf319ecbc61d74"),
    "hunyuanvideo-1.5-720p-t2v": ("hunyuanvideo-community/HunyuanVideo-1.5-Diffusers-720p_t2v", "f4dbc4a1efa4ac8ea56680cdf79d9f455105e814"),
}
# H3's fixed tokenizer JSON is a 7 MB ordinary Git blob, not a weight.
MAX_BLOB = 8 * 1024 * 1024
MAX_TREE = 4 * 1024 * 1024
TEXT_SUFFIXES = {".json", ".txt", ".jinja", ".yaml", ".yml"}


def fetch(url: str, limit: int) -> tuple[bytes, str | None]:
    if urllib.parse.urlsplit(url).scheme != "https" or urllib.parse.urlsplit(url).netloc != "huggingface.co":
        raise ValueError("only public Hugging Face HTTPS metadata is allowed")
    request = urllib.request.Request(url, headers={"User-Agent": "MediaCenter-recipe-audit/1"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                data = response.read(limit + 1)
                if len(data) > limit:
                    raise ValueError("metadata response exceeds bounded size")
                return data, response.headers.get("Link")
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
    raise AssertionError("unreachable")


def safe_path(path: str) -> bool:
    return (isinstance(path, str) and bool(path) and not path.startswith("/") and
            "\\" not in path and all(part not in {"", ".", ".."} for part in path.split("/")) and
            re.fullmatch(r"[A-Za-z0-9_./-]+", path) is not None)


def describe_file(item: dict, base: str) -> tuple[dict, dict | None]:
    path, size = item["path"], item["size"]
    if not safe_path(path) or type(size) is not int or size <= 0:
        raise ValueError("invalid file path/size")
    url = base + path
    content = None
    lfs = item.get("lfs")
    if lfs is not None:
        digest = lfs.get("oid", "")
        if lfs.get("size") != size or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("invalid LFS size/SHA-256")
    else:
        if size > MAX_BLOB or PurePosixPath(path).suffix not in TEXT_SUFFIXES:
            raise ValueError("refusing non-small/non-text blob: " + path)
        blob, _ = fetch(url, size)
        if len(blob) != size or hashlib.sha1(b"blob " + str(size).encode() + b"\0" + blob).hexdigest() != item["oid"]:
            raise ValueError("Git blob content mismatch: " + path)
        digest = hashlib.sha256(blob).hexdigest()
        if path.endswith(".index.json"):
            content = json.loads(blob)
    return {"relative_path": path, "byte_size": size, "sha256": digest, "url": url}, content


def build(entry: dict) -> dict:
    key = entry["catalog_key"]
    model_id, revision = PINS[key]
    if (entry["model_id"], entry["recommended_revision"]) != (model_id, revision):
        raise ValueError("catalog differs from approved fixed video revision")
    url = f"https://huggingface.co/api/models/{model_id}/tree/{revision}?recursive=true"
    # A truncated tree is not an acceptable manifest. Require an explicit tool
    # update/review if these fixed trees ever exceed one metadata page.
    raw, link = fetch(url, MAX_TREE)
    if link:
        raise ValueError("paginated tree needs explicit review; refusing incomplete manifest")
    tree = json.loads(raw)
    selected = [item for item in tree if item["type"] == "file" and any(
        fnmatch.fnmatchcase(item["path"], pattern) for pattern in entry["download_allow_patterns"])]
    selected.sort(key=lambda item: item["path"])
    paths = {item["path"] for item in selected}
    if len(paths) != len(selected) or not set(entry["required_files"]) <= paths:
        raise ValueError("missing required files or duplicate paths")
    base = f"https://huggingface.co/{model_id}/resolve/{revision}/"
    with ThreadPoolExecutor(max_workers=4) as pool:
        pairs = list(pool.map(lambda item: describe_file(item, base), selected))
    for record, index in pairs:
        if index is not None:
            parent = PurePosixPath(record["relative_path"]).parent
            weights = index.get("weight_map", {})
            if not weights or any(not safe_path(name) or str(parent / name) not in paths
                                  for name in weights.values()):
                raise ValueError("index references an absent/unsafe weight shard")
    return {"catalog_key": key, "model_id": model_id, "revision": revision,
            "tree_url": url, "files": [record for record, _ in pairs]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key", choices=tuple(PINS))
    args = parser.parse_args()
    catalog = json.loads((Path(__file__).resolve().parents[1] / "deploy/model_catalog.json").read_text(encoding="utf-8"))
    entries = [entry for entry in catalog["models"] if entry["catalog_key"] in PINS and
               (args.key is None or entry["catalog_key"] == args.key)]
    print(json.dumps([build(entry) for entry in entries], ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
