#!/usr/bin/env python3
"""Convert a frozen single-platform OCI archive for Docker overlay2 loading."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
import zlib
from pathlib import Path


DIGEST = re.compile(r"sha256:([0-9a-f]{64})\Z")
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
OCI_CONFIG = "application/vnd.oci.image.config.v1+json"
LAYER = "application/vnd.oci.image.layer.v1.tar"
LAYER_GZIP = LAYER + "+gzip"
LAYER_ZSTD = LAYER + "+zstd"


def strict_json(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value
    return json.loads(raw, object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite JSON")))


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode()


def digest_hex(value):
    matched = DIGEST.fullmatch(value) if isinstance(value, str) else None
    if not matched:
        raise ValueError("invalid sha256 digest")
    return matched.group(1)


def checked_members(source):
    result = {}
    for member in source.getmembers():
        if (member.name.startswith("/") or ".." in Path(member.name).parts
                or not (member.isfile() or member.isdir()) or member.name in result):
            raise ValueError("unsafe OCI archive member")
        result[member.name] = member
    return result


def read_blob(source, members, descriptor, limit=4 * 1024 * 1024):
    name = "blobs/sha256/" + digest_hex(descriptor.get("digest"))
    member = members.get(name)
    if (member is None or not member.isfile() or member.size != descriptor.get("size")
            or member.size > limit):
        raise ValueError("OCI descriptor mismatch")
    stream = source.extractfile(member)
    raw = stream.read() if stream else b""
    if len(raw) != member.size or hashlib.sha256(raw).hexdigest() != name.rsplit("/", 1)[1]:
        raise ValueError("OCI blob digest mismatch")
    return raw


def tar_info(name, size):
    info = tarfile.TarInfo(name)
    info.size = size
    info.mtime = info.uid = info.gid = 0
    info.mode = 0o644
    info.uname = info.gname = ""
    return info


def expand_layer(source, members, descriptor, expected_diff_id, destination):
    name = "blobs/sha256/" + digest_hex(descriptor.get("digest"))
    member = members.get(name)
    if member is None or not member.isfile() or member.size != descriptor.get("size"):
        raise ValueError("OCI layer descriptor mismatch")
    stream = source.extractfile(member)
    if stream is None:
        raise ValueError("OCI layer missing")
    media = descriptor.get("mediaType")
    compressed_hash, expanded_hash, compressed_size, expanded_size = hashlib.sha256(), hashlib.sha256(), 0, 0
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS) if media == LAYER_GZIP else None
    if media not in {LAYER, LAYER_GZIP, LAYER_ZSTD}:
        raise ValueError("unsupported OCI layer media type")
    compressed_temporary = destination.with_name(destination.name + ".zstd") if media == LAYER_ZSTD else None
    try:
        write_target = compressed_temporary if compressed_temporary else destination
        with write_target.open("xb") as output:
            while True:
                block = stream.read(1024 * 1024)
                if not block:
                    break
                compressed_hash.update(block); compressed_size += len(block)
                if compressed_temporary:
                    output.write(block)
                else:
                    value = decoder.decompress(block) if decoder else block
                    if value:
                        output.write(value); expanded_hash.update(value); expanded_size += len(value)
            if decoder:
                value = decoder.flush()
                if value:
                    output.write(value); expanded_hash.update(value); expanded_size += len(value)
                if not decoder.eof or decoder.unused_data:
                    raise ValueError("invalid gzip layer")
        if compressed_temporary:
            with destination.open("xb") as output:
                completed = subprocess.run(["/usr/bin/zstd", "-dc", "--no-progress", str(compressed_temporary)],
                                           stdout=output, stderr=subprocess.PIPE, timeout=3600, check=False)
            if completed.returncode or len(completed.stderr) > 8192:
                raise ValueError("invalid zstd layer")
            with destination.open("rb") as expanded_stream:
                for block in iter(lambda: expanded_stream.read(1024 * 1024), b""):
                    expanded_hash.update(block); expanded_size += len(block)
    except BaseException:
        if destination.exists():
            destination.unlink()
        raise
    finally:
        if compressed_temporary is not None and compressed_temporary.exists():
            compressed_temporary.unlink()
    if compressed_size != member.size or compressed_hash.hexdigest() != digest_hex(descriptor["digest"]):
        raise ValueError("OCI layer blob mismatch")
    if expanded_hash.hexdigest() != digest_hex(expected_diff_id):
        raise ValueError("layer diff ID mismatch")
    return expanded_size


def file_sha256(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def convert(source_path, output_path):
    source_path, output_path = Path(source_path).resolve(), Path(output_path).resolve()
    if source_path == output_path or not source_path.is_file() or output_path.exists():
        raise ValueError("source/output path invalid")
    with tarfile.open(source_path, "r:*") as source:
        members = checked_members(source)
        index_member = members.get("index.json")
        if index_member is None or not index_member.isfile():
            raise ValueError("OCI index missing")
        index = strict_json(source.extractfile(index_member).read())
        manifests = index.get("manifests") if isinstance(index, dict) else None
        if not isinstance(manifests, list) or len(manifests) != 1 or manifests[0].get("mediaType") != OCI_MANIFEST:
            raise ValueError("single OCI manifest required")
        manifest = strict_json(read_blob(source, members, manifests[0]))
        config_descriptor, layers = manifest.get("config"), manifest.get("layers")
        if (manifest.get("schemaVersion") != 2 or manifest.get("mediaType") != OCI_MANIFEST
                or not isinstance(config_descriptor, dict) or config_descriptor.get("mediaType") != OCI_CONFIG
                or not isinstance(layers, list) or not layers):
            raise ValueError("OCI manifest invalid")
        config_raw = read_blob(source, members, config_descriptor)
        config = strict_json(config_raw)
        diff_ids = config.get("rootfs", {}).get("diff_ids") if isinstance(config, dict) else None
        if not isinstance(diff_ids, list) or len(diff_ids) != len(layers):
            raise ValueError("OCI rootfs invalid")
        layer_files = [output_path.with_name(output_path.name + ".layer-" + digest_hex(value) + ".partial")
                       for value in diff_ids]
        if any(path.exists() for path in layer_files):
            raise ValueError("partial layer output already exists")
        try:
            expanded_sizes = [expand_layer(source, members, layer, diff_id, path)
                              for layer, diff_id, path in zip(layers, diff_ids, layer_files)]
        except BaseException:
            for path in layer_files:
                if path.exists():
                    path.unlink()
            raise

    config_name = "blobs/sha256/" + digest_hex(config_descriptor["digest"])
    layer_names = ["blobs/sha256/" + digest_hex(value) for value in diff_ids]
    layer_sources = {value: {"mediaType": LAYER, "size": size, "digest": value}
                     for value, size in zip(diff_ids, expanded_sizes)}
    record = [{"Config": config_name, "RepoTags": None,
               "Layers": layer_names, "LayerSources": layer_sources}]
    manifest_raw = canonical(record)
    temporary = output_path.with_name(output_path.name + ".partial")
    if temporary.exists():
        raise ValueError("partial output already exists")
    try:
        with tarfile.open(temporary, "w", format=tarfile.PAX_FORMAT) as target:
            target.addfile(tar_info("manifest.json", len(manifest_raw)), io.BytesIO(manifest_raw))
            target.addfile(tar_info(config_name, len(config_raw)), io.BytesIO(config_raw))
            for name, path, size in zip(layer_names, layer_files, expanded_sizes):
                with path.open("rb") as stream:
                    target.addfile(tar_info(name, size), stream)
        os.replace(temporary, output_path)
    except BaseException:
        if temporary.exists():
            temporary.unlink()
        raise
    finally:
        for path in layer_files:
            if path.exists():
                path.unlink()
    return {"schema": "mc.oci-to-docker-archive/1", "source": str(source_path),
            "output": str(output_path), "output_bytes": output_path.stat().st_size,
            "output_sha256": file_sha256(output_path), "manifest_digest": manifests[0]["digest"],
            "config_digest": config_descriptor["digest"], "layer_count": len(layers), "status": "passed"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("output")
    parser.add_argument("--evidence")
    args = parser.parse_args()
    result = convert(args.source, args.output)
    raw = canonical(result) + b"\n"
    if args.evidence:
        evidence = Path(args.evidence)
        if evidence.exists():
            raise ValueError("evidence output already exists")
        evidence.write_bytes(raw)
    print(raw.decode(), end="")


if __name__ == "__main__":
    main()
