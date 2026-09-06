#!/usr/bin/env python3
"""Build one offline OCI image derived from an already verified parent OCI.

The tool never resolves packages or contacts a registry.  It verifies an exact
wheel/hash lock, creates a system-site-packages virtual environment by running
the already imported parent image with networking disabled, and appends
deterministic dependency and MediaCenter SDK layers.  ``--sdk-only`` refreshes
the worker SDK on an existing derived runtime without rebuilding dependencies.
"""
from __future__ import annotations

import argparse
import email
import gzip
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
import zipfile
from pathlib import Path, PurePosixPath


OCI_LAYOUT = {"imageLayoutVersion": "1.0.0"}
MANIFEST_TYPE = "application/vnd.oci.image.manifest.v1+json"
CONFIG_TYPE = "application/vnd.oci.image.config.v1+json"
LAYER_TYPE = "application/vnd.oci.image.layer.v1.tar+gzip"
WORKER_ENTRYPOINTS = ("mediacenter.image_worker_cli", "mediacenter.worker_cli",
                     "mediacenter.video_worker_cli", "mediacenter.audio_worker_cli")
LOCK_LINE = re.compile(
    r"(?P<name>[A-Za-z0-9_.-]+)==(?P<version>[^\s]+)\s+"
    r"--hash=sha256:(?P<digest>[0-9a-f]{64})"
)


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode()


def sha_file(path: Path) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def require_new(path: Path, *, parent=True) -> Path:
    value = path.resolve()
    if value.exists():
        raise ValueError(f"output already exists: {value}")
    if parent and not value.parent.is_dir():
        raise ValueError(f"output parent is missing: {value.parent}")
    return value


def require_regular(path: Path) -> Path:
    value = path.resolve(strict=True)
    if not value.is_file() or value.is_symlink():
        raise ValueError(f"regular file required: {value}")
    return value


def require_directory(path: Path) -> Path:
    value = path.resolve(strict=True)
    if not value.is_dir() or value.is_symlink():
        raise ValueError(f"directory required: {value}")
    return value


def wheel_identity(path: Path) -> tuple[str, str]:
    with zipfile.ZipFile(path) as archive:
        metadata = [name for name in archive.namelist()
                    if name.endswith(".dist-info/METADATA")]
        if len(metadata) != 1:
            raise ValueError(f"wheel metadata invalid: {path.name}")
        parsed = email.message_from_bytes(archive.read(metadata[0]))
    name, version = parsed.get("Name"), parsed.get("Version")
    if not name or not version:
        raise ValueError(f"wheel identity missing: {path.name}")
    return normalized(name), version


def verify_lock(requirements: Path, wheels: Path) -> list[dict]:
    locked: dict[str, dict] = {}
    for raw in requirements.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = LOCK_LINE.fullmatch(line)
        if not match:
            raise ValueError(f"invalid locked requirement: {line}")
        key = normalized(match.group("name"))
        if key in locked:
            raise ValueError(f"duplicate locked distribution: {key}")
        locked[key] = {"name": match.group("name"),
                       "version": match.group("version"),
                       "sha256": match.group("digest")}
    if not locked:
        raise ValueError("dependency lock is empty")

    observed: dict[str, dict] = {}
    for wheel in sorted(wheels.glob("*.whl")):
        if wheel.is_symlink() or not wheel.is_file():
            raise ValueError(f"wheel is not a regular file: {wheel}")
        key, version = wheel_identity(wheel)
        digest, size = sha_file(wheel)
        if key in observed:
            raise ValueError(f"duplicate wheel distribution: {key}")
        observed[key] = {"filename": wheel.name, "version": version,
                         "sha256": digest, "byte_size": size}
    if set(observed) != set(locked):
        raise ValueError("wheel set differs from dependency lock")
    result = []
    for key in sorted(locked):
        expected, actual = locked[key], observed[key]
        if (actual["version"] != expected["version"]
                or actual["sha256"] != expected["sha256"]):
            raise ValueError(f"wheel identity mismatch: {key}")
        result.append({"name": expected["name"], **actual})
    return result


def docker_image_id(reference: str) -> str:
    process = subprocess.run(
        ["docker", "image", "inspect", reference, "--format", "{{.Id}}"],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        timeout=30,
    )
    return process.stdout.strip()


def create_environment(parent: str, expected_image_id: str, wheels: Path,
                       requirements: Path, target: Path) -> None:
    if docker_image_id(parent) != expected_image_id:
        raise ValueError("imported parent image identity mismatch")
    target.mkdir(parents=True, mode=0o755)
    command = (
        "/opt/python/bin/python3.11 -m venv --system-site-packages "
        "/opt/krea-runtime && "
        "/opt/krea-runtime/bin/python -m pip install --no-index --no-deps "
        "--no-cache-dir --require-hashes --find-links=/offline/wheels "
        "-r /offline/krea2.requirements.txt && "
        "/opt/krea-runtime/bin/python -m pip check"
    )
    subprocess.run([
        "docker", "run", "--rm", "--user", "0:0", "--network", "none",
        "--env", "PIP_NO_INDEX=1", "--env", "PIP_DISABLE_PIP_VERSION_CHECK=1",
        "--volume", f"{target}:/opt/krea-runtime",
        "--volume", f"{wheels}:/offline/wheels:ro",
        "--volume", f"{requirements}:/offline/krea2.requirements.txt:ro",
        "--entrypoint", "/bin/bash", parent, "-lc", command,
    ], check=True, timeout=900)


def copy_sdk(source: Path, target: Path) -> list[dict]:
    destination = target / "opt" / "mediacenter" / "mediacenter"
    destination.mkdir(parents=True, mode=0o755)
    copied = []
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if "__pycache__" in relative.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        if path.is_symlink():
            raise ValueError(f"SDK symlink rejected: {relative.as_posix()}")
        output = destination / relative
        if path.is_dir():
            output.mkdir(mode=0o755, exist_ok=True)
        elif path.is_file():
            output.parent.mkdir(parents=True, exist_ok=True)
            with path.open("rb") as reader, output.open("xb") as writer:
                shutil.copyfileobj(reader, writer, 1024 * 1024)
            os.chmod(output, stat.S_IMODE(path.stat().st_mode))
            digest, size = sha_file(output)
            copied.append({"path": (PurePosixPath("mediacenter") / relative).as_posix(),
                           "sha256": digest, "byte_size": size})
        else:
            raise ValueError(f"SDK special file rejected: {relative.as_posix()}")
    if not copied:
        raise ValueError("SDK source is empty")
    return copied


def tar_entry(archive: tarfile.TarFile, path: Path, root: Path,
              *, uid: int, gid: int) -> None:
    relative = path.relative_to(root).as_posix()
    info = tarfile.TarInfo(relative)
    status = path.lstat()
    info.mtime = 0
    info.uid, info.gid, info.uname, info.gname = uid, gid, "", ""
    info.mode = stat.S_IMODE(status.st_mode)
    if path.is_symlink():
        target = os.readlink(path)
        valid_python = (relative.startswith("opt/krea-runtime/bin/")
                        and target in {"/opt/python/bin/python3.11", "python", "python3", "python3.11"})
        valid_lib = relative == "opt/krea-runtime/lib64" and target == "lib"
        if not (valid_python or valid_lib):
            raise ValueError(f"unexpected layer symlink: {relative} -> {target}")
        info.type, info.linkname, info.size = tarfile.SYMTYPE, target, 0
        archive.addfile(info)
    elif path.is_dir():
        info.type, info.size = tarfile.DIRTYPE, 0
        archive.addfile(info)
    elif path.is_file():
        info.type, info.size = tarfile.REGTYPE, status.st_size
        with path.open("rb") as stream:
            archive.addfile(info, stream)
    else:
        raise ValueError(f"special layer member rejected: {relative}")


def make_layer(stage: Path, output: Path, name: str, *, uid: int, gid: int) -> dict:
    raw = require_new(output.with_suffix(output.suffix + ".raw"))
    blob = require_new(output)
    with raw.open("xb") as stream:
        with tarfile.open(fileobj=stream, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for path in sorted(stage.rglob("*"), key=lambda item: item.relative_to(stage).as_posix()):
                tar_entry(archive, path, stage, uid=uid, gid=gid)
    diff_digest, _ = sha_file(raw)
    with raw.open("rb") as source, blob.open("xb") as destination:
        with gzip.GzipFile(filename="", mode="wb", fileobj=destination,
                           compresslevel=6, mtime=0) as compressed:
            shutil.copyfileobj(source, compressed, 1024 * 1024)
    digest, size = sha_file(blob)
    return {"name": name, "path": blob, "digest": "sha256:" + digest,
            "diff_id": "sha256:" + diff_digest, "size": size,
            "mediaType": LAYER_TYPE}


def read_json_blob(layout: Path, descriptor: dict) -> dict:
    digest = descriptor.get("digest", "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("OCI descriptor digest invalid")
    path = layout / "blobs" / "sha256" / digest.split(":", 1)[1]
    raw = path.read_bytes()
    if len(raw) != descriptor.get("size") or hashlib.sha256(raw).hexdigest() != digest.split(":", 1)[1]:
        raise ValueError("OCI descriptor identity mismatch")
    return json.loads(raw)


def extract_parent(source: Path, layout: Path) -> tuple[dict, dict, dict]:
    layout.mkdir(mode=0o700)
    seen = set()
    with tarfile.open(source, "r:") as archive:
        for member in archive:
            name = member.name.removeprefix("./")
            if name in seen or (name not in {"oci-layout", "index.json", "blobs", "blobs/sha256"}
                                and not re.fullmatch(r"blobs/sha256/[0-9a-f]{64}", name)):
                raise ValueError(f"parent OCI member invalid: {name}")
            seen.add(name)
            target = layout / PurePosixPath(name)
            if member.isdir() and name in {"blobs", "blobs/sha256"}:
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise ValueError(f"parent OCI member type invalid: {name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            content = archive.extractfile(member)
            if content is None:
                raise ValueError(f"parent OCI member missing: {name}")
            with content, target.open("xb") as output:
                shutil.copyfileobj(content, output, 1024 * 1024)
    if json.loads((layout / "oci-layout").read_bytes()) != OCI_LAYOUT:
        raise ValueError("parent OCI layout invalid")
    index = json.loads((layout / "index.json").read_bytes())
    if len(index.get("manifests", [])) != 1:
        raise ValueError("parent OCI must contain exactly one manifest")
    manifest = read_json_blob(layout, index["manifests"][0])
    config = read_json_blob(layout, manifest["config"])
    return index, manifest, config


def write_blob(layout: Path, raw: bytes) -> dict:
    digest = hashlib.sha256(raw).hexdigest()
    target = layout / "blobs" / "sha256" / digest
    with target.open("xb") as stream:
        stream.write(raw)
    return {"digest": "sha256:" + digest, "size": len(raw)}


def add_file(archive: tarfile.TarFile, path: Path, name: str) -> None:
    info = tarfile.TarInfo(name)
    info.size, info.mode, info.mtime = path.stat().st_size, 0o600, 0
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    with path.open("rb") as stream:
        archive.addfile(info, stream)


def assemble(parent: Path, layout: Path, layers: list[dict], output: Path,
             created: str, *, expected_parent_image_id: str | None = None,
             python_prefix: str = "/opt/krea-runtime",
             worker_entrypoint: str = "mediacenter.image_worker_cli") -> dict:
    if python_prefix not in {"/opt/krea-runtime", "/opt/python"}:
        raise ValueError("derived runtime Python prefix is not approved")
    if worker_entrypoint not in WORKER_ENTRYPOINTS:
        raise ValueError("derived runtime Worker entrypoint is not approved")
    index, manifest, config = extract_parent(parent, layout)
    parent_config = manifest["config"]["digest"]
    if expected_parent_image_id is not None:
        expected = expected_parent_image_id
        if not expected.startswith("sha256:"):
            expected = "sha256:" + expected
        if parent_config != expected:
            raise ValueError("parent OCI image identity mismatch")
    for layer in layers:
        destination = layout / "blobs" / "sha256" / layer["digest"].split(":", 1)[1]
        if not destination.exists():
            os.link(layer["path"], destination)
    parent_manifest = index["manifests"][0]["digest"]
    environment = list(config.get("config", {}).get("Env") or [])
    environment = [value for value in environment if not value.startswith("PATH=")]
    environment.insert(
        0,
        f"PATH={python_prefix}/bin:/opt/python/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    )
    image_config = json.loads(json.dumps(config))
    image_config.update(created=created, architecture="amd64", os="linux")
    actual = image_config.setdefault("config", {})
    for key in ("Volumes", "OnBuild", "ExposedPorts"):
        actual.pop(key, None)
    entrypoint = [python_prefix + "/bin/python", "-B", "-u", "-m",
                  worker_entrypoint]
    actual.update(Env=environment, Entrypoint=entrypoint, Cmd=None,
                  WorkingDir="/opt/mediacenter", User="1000:1000")
    rootfs = image_config.get("rootfs", {})
    if rootfs.get("type") != "layers" or len(rootfs.get("diff_ids", [])) != len(manifest.get("layers", [])):
        raise ValueError("parent rootfs contract invalid")
    rootfs["diff_ids"].extend(layer["diff_id"] for layer in layers)
    history = image_config.setdefault("history", [])
    history.extend({"created": created, "created_by": "MediaCenter derived OCI builder",
                    "comment": layer["name"]} for layer in layers)
    config_descriptor = write_blob(layout, canonical(image_config))
    layer_descriptors = [{"mediaType": row["mediaType"], "digest": row["digest"],
                          "size": row["size"]} for row in layers]
    image_manifest = {"schemaVersion": 2, "mediaType": MANIFEST_TYPE,
                      "config": {"mediaType": CONFIG_TYPE, **config_descriptor},
                      "layers": list(manifest["layers"]) + layer_descriptors}
    manifest_descriptor = write_blob(layout, canonical(image_manifest))
    final_index = {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json",
                   "manifests": [{"mediaType": MANIFEST_TYPE, **manifest_descriptor,
                                  "platform": {"architecture": "amd64", "os": "linux"}}]}
    (layout / "index.json").write_bytes(canonical(final_index))

    partial = require_new(output.with_suffix(output.suffix + ".partial"))
    with partial.open("xb") as stream:
        with tarfile.open(fileobj=stream, mode="w", format=tarfile.PAX_FORMAT) as archive:
            add_file(archive, layout / "oci-layout", "oci-layout")
            add_file(archive, layout / "index.json", "index.json")
            for blob in sorted((layout / "blobs" / "sha256").iterdir(), key=lambda item: item.name):
                add_file(archive, blob, "blobs/sha256/" + blob.name)
    os.replace(partial, output)
    digest, size = sha_file(output)
    return {"parent_manifest_digest": parent_manifest,
            "parent_config_digest": parent_config,
            "manifest_digest": manifest_descriptor["digest"],
            "config_digest": config_descriptor["digest"],
            "archive_sha256": digest, "archive_bytes": size,
            "entrypoint": entrypoint, "command": [], "environment": environment}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", required=True)
    parser.add_argument("--parent-oci", required=True, type=Path)
    parser.add_argument("--parent-reference", required=True)
    parser.add_argument("--parent-image-id", required=True)
    parser.add_argument("--wheels", type=Path)
    parser.add_argument("--requirements", type=Path)
    parser.add_argument("--sdk-only", action="store_true")
    parser.add_argument("--worker-entrypoint", choices=WORKER_ENTRYPOINTS,
                        default="mediacenter.image_worker_cli")
    parser.add_argument(
        "--python-prefix", choices=("/opt/krea-runtime", "/opt/python"),
        default="/opt/krea-runtime",
        help="Approved Python prefix already present in the parent image.",
    )
    parser.add_argument("--sdk-source", required=True, type=Path)
    parser.add_argument("--work-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--created", required=True)
    args = parser.parse_args()

    parent = require_regular(args.parent_oci)
    sdk_source = require_directory(args.sdk_source)
    work = require_new(args.work_root)
    output, evidence = require_new(args.output), require_new(args.evidence)
    work.mkdir(mode=0o700)
    locked = []
    layers = []
    if not args.sdk_only:
        if args.python_prefix != "/opt/krea-runtime":
            raise ValueError("dependency builds require the isolated Krea runtime prefix")
        if args.wheels is None or args.requirements is None:
            raise ValueError("wheels and requirements are required without --sdk-only")
        wheels = require_directory(args.wheels)
        requirements = require_regular(args.requirements)
        locked = verify_lock(requirements, wheels)
        dependency_root = work / "dependency-layer"
        runtime = dependency_root / "opt" / "krea-runtime"
        create_environment(args.parent_reference, args.parent_image_id,
                           wheels, requirements, runtime)
        layers.append(make_layer(
            dependency_root, work / "krea-dependencies.tar.gz",
            "Krea 2 locked dependency environment", uid=0, gid=0,
        ))
    sdk_root = work / "sdk-layer"
    sdk = copy_sdk(sdk_source, sdk_root)
    layers.append(make_layer(
        sdk_root, work / "mediacenter-sdk.tar.gz",
        "MediaCenter image worker SDK", uid=1000, gid=1000,
    ))
    result = assemble(
        parent, work / "layout", layers, output, args.created,
        expected_parent_image_id=args.parent_image_id,
        python_prefix=args.python_prefix,
        worker_entrypoint=args.worker_entrypoint,
    )
    report = {"schema": "mc.derived-oci-build/1", "status": "passed",
              "mode": "sdk-only" if args.sdk_only else "dependencies-and-sdk",
              "parent_oci": str(parent), "parent_reference": args.parent_reference,
              "parent_image_id": args.parent_image_id, "wheels": locked,
              "python_prefix": args.python_prefix,
              "sdk_files": sdk, "sdk_digest": layers[-1]["diff_id"].split(":", 1)[1],
              "layers": [{key: value for key, value in layer.items() if key != "path"}
                         for layer in layers], **result}
    evidence.write_bytes(canonical(report) + b"\n")
    print(canonical({"status": report["status"], "manifest_digest": result["manifest_digest"],
                     "config_digest": result["config_digest"],
                     "archive_sha256": result["archive_sha256"],
                     "archive_bytes": result["archive_bytes"],
                     "sdk_digest": report["sdk_digest"]}).decode())


if __name__ == "__main__":
    main()
