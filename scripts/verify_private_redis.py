#!/usr/bin/env python3
"""Verify the private Redis socket and publisher ACL without exposing secrets."""
import argparse
import hashlib
import json
import stat
from pathlib import Path

import redis


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    socket_path = root / "run/redis.sock"
    secret_path = root / "secrets/manager.secret"
    evidence = json.loads((root / "preparation-evidence.json").read_text(encoding="utf-8"))
    secret = secret_path.read_text(encoding="ascii").strip()
    if hashlib.sha256(secret.encode()).hexdigest() != evidence["manager_secret_sha256"]:
        raise ValueError("publisher secret changed")
    info = socket_path.stat()
    if not stat.S_ISSOCK(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("private Redis socket contract changed")
    client = redis.Redis(unix_socket_path=str(socket_path), username=evidence["username"],
                         password=secret, decode_responses=True, socket_timeout=2)
    acl = client.execute_command("ACL", "GETUSER", evidence["username"])
    client.close()
    if not acl:
        raise ValueError("publisher ACL unavailable")
    aof = sorted(str(path.relative_to(root)) for path in (root / "data").rglob("*") if path.is_file())
    print(json.dumps({"schema": "mc.private-redis-verification/1", "status": "passed",
                      "socket_mode": "0700", "username": evidence["username"],
                      "secret_sha256": evidence["manager_secret_sha256"], "aof_files": aof},
                     sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
