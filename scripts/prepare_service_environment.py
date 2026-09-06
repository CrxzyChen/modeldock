#!/usr/bin/env python3
"""Split the legacy inline API key into private files for the MC-044 service."""
from __future__ import annotations

import argparse
import os
import re
from pathlib import Path


KEY = re.compile(r"[A-Z_][A-Z0-9_]*")


def read_environment(path: Path) -> dict[str, str]:
    result = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError("legacy environment contains a non-assignment")
        key, value = line.split("=", 1)
        if not KEY.fullmatch(key) or key in result or "\x00" in value or "\n" in value:
            raise ValueError("legacy environment contains an unsafe assignment")
        result[key] = value
    return result


def write_new(path: Path, raw: bytes) -> None:
    if path.exists() or not path.parent.is_dir():
        raise ValueError("service environment output must be new")
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--api-key-file", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--runtime-config", required=True)
    parser.add_argument("--model-catalog", required=True)
    parser.add_argument("--gpu-pool", required=True)
    parser.add_argument("--gpu-uuid-pool", required=True)
    args = parser.parse_args()
    source = Path(args.source).resolve()
    if not source.is_file() or any(path.is_symlink() for path in (source, *source.parents)):
        raise ValueError("legacy environment source is invalid")
    values = read_environment(source)
    secret = values.pop("MEDIACENTER_API_KEY", None)
    if not secret or len(secret) > 4096:
        raise ValueError("legacy API key is missing or invalid")
    values.update({
        "MEDIACENTER_API_KEY_FILE": str(Path(args.api_key_file).resolve()),
        "MEDIACENTER_DB": str(Path(args.database).resolve()),
        "MEDIACENTER_RUNTIME_CONFIG": str(Path(args.runtime_config).resolve()),
        "MEDIACENTER_MODEL_CATALOG": str(Path(args.model_catalog).resolve()),
        "MEDIACENTER_GPU_POOL": args.gpu_pool,
        "MEDIACENTER_GPU_UUID_POOL": args.gpu_uuid_pool,
        "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"})
    write_new(Path(args.api_key_file).resolve(), (secret + "\n").encode("utf-8"))
    body = "".join(f"{key}={values[key]}\n" for key in sorted(values))
    write_new(Path(args.environment).resolve(), body.encode("utf-8"))
    print(f"prepared private API key and {len(values)} non-secret environment assignments")


if __name__ == "__main__":
    main()
