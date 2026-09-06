#!/usr/bin/env python3
"""Create exclusive private Redis configuration and publisher credentials."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets


def write(path: Path, data: bytes, mode=0o600):
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
    finally:
        os.close(descriptor)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--redis-template", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.absolute()
    template = args.redis_template.absolute()
    if (root.exists() or not template.is_file() or template.is_symlink()
            or any(path.is_symlink() for path in template.parents)):
        raise ValueError("private_redis_path_invalid")
    root.mkdir(mode=0o700)
    for name in ("run", "data", "secrets", "publisher", "packages", "images"):
        (root / name).mkdir(mode=0o700)
    manager = secrets.token_urlsafe(48)
    seed = secrets.token_urlsafe(64)
    # RedisEndpoint's credential contract is the exact token bytes.  A text
    # newline would make the Server reject its own private credential file.
    write(root / "secrets/manager.secret", manager.encode("ascii"))
    write(root / "secrets/seed.secret", seed.encode("ascii"))
    manager_hash = hashlib.sha256(manager.encode("ascii")).hexdigest()
    acl = ("user default off\n"
           "user private-publisher on sanitize-payload #" + manager_hash
           + " -@all +acl|getuser +acl|setuser +acl|save\n")
    write(root / "data/mediacenter-redis.acl", acl.encode("ascii"))
    config = template.read_bytes()
    expected = b"unixsocket /run/mediacenter/redis.sock\n"
    if (b"port 0\n" not in config or expected not in config
            or b"aclfile /var/lib/mediacenter-redis/mediacenter-redis.acl\n" not in config):
        raise ValueError("private_redis_template_changed")
    write(root / "redis.conf", config)
    evidence = {"schema": "mc.private-redis-material/1", "username": "private-publisher",
                "manager_secret_sha256": manager_hash,
                "seed_sha256": hashlib.sha256(seed.encode("ascii")).hexdigest(),
                "config_sha256": hashlib.sha256(config).hexdigest(),
                "socket": str(root / "run/redis.sock")}
    write(root / "preparation-evidence.json",
          (json.dumps(evidence, sort_keys=True, separators=(",", ":")) + "\n").encode())
    print(json.dumps(evidence, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
