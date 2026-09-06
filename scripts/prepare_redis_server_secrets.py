#!/usr/bin/env python3
"""Normalize the first private Redis material without exposing its secrets."""
from __future__ import annotations

import argparse
import hashlib
import os
import re
from pathlib import Path


def copy_token(source: Path, target: Path) -> str:
    if target.exists() or not source.is_file() or source.is_symlink():
        raise ValueError("secret source or exclusive target is invalid")
    raw = source.read_bytes()
    token = raw[:-1] if raw.endswith(b"\n") else raw
    if raw not in {token, token + b"\n"} or not re.fullmatch(rb"[A-Za-z0-9_-]{32,256}", token):
        raise ValueError("secret token is invalid")
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(token); stream.flush(); os.fsync(stream.fileno())
    return hashlib.sha256(token).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    args = parser.parse_args()
    root = Path(args.root).resolve() / "secrets"
    manager = copy_token(root / "manager.secret", root / "server-manager.secret")
    seed = copy_token(root / "seed.secret", root / "server-seed.secret")
    print("prepared two newline-free Server secret files",
          "manager_sha256=" + manager, "seed_sha256=" + seed)


if __name__ == "__main__":
    main()
