#!/usr/bin/env python3
"""Freeze the exact installed Ubuntu C compiler closure into a reviewed layer root."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def package_version(name):
    result = subprocess.run(["dpkg-query", "-W", "-f=${Version}", name], check=True,
                            capture_output=True, text=True)
    return result.stdout


def package_paths(name):
    result = subprocess.run(["dpkg-query", "-L", name], check=True, capture_output=True, text=True)
    return sorted(set(result.stdout.splitlines()))


def copy_entry(source, target):
    info = os.lstat(source)
    if stat.S_ISDIR(info.st_mode):
        target.mkdir(parents=True, exist_ok=True)
        os.chmod(target, stat.S_IMODE(info.st_mode)); return
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        if source.is_symlink() and target.is_symlink() and os.readlink(source) == os.readlink(target): return
        if source.is_file() and target.is_file() and sha(source) == sha(target): return
        raise ValueError("compiler_package_path_conflict")
    if stat.S_ISLNK(info.st_mode):
        raw = os.readlink(source)
        if "\x00" in raw: raise ValueError("compiler_symlink_invalid")
        target.symlink_to(raw); return
    if stat.S_ISREG(info.st_mode):
        shutil.copyfile(source, target); os.chmod(target, stat.S_IMODE(info.st_mode)); return
    raise ValueError("compiler_special_file_rejected")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    lock = json.loads(args.lock.resolve(strict=True).read_text(encoding="utf-8"))
    packages = lock.get("packages")
    if lock.get("schema") != "mc.host-compiler-packages/1" or not isinstance(packages, dict) or not packages:
        raise ValueError("compiler_lock_invalid")
    output = args.output.resolve()
    if output.exists() or args.evidence.exists(): raise ValueError("compiler_output_not_exclusive")
    output.mkdir(mode=0o700)
    for name, version in sorted(packages.items()):
        if package_version(name) != version: raise ValueError("compiler_package_version_changed:" + name)
        for raw in package_paths(name):
            source = Path(raw)
            if not source.is_absolute() or not source.exists() and not source.is_symlink():
                raise ValueError("compiler_package_path_missing:" + name)
            if source == Path("/"): continue
            copy_entry(source, output / source.relative_to("/"))
    cc = output / "usr/bin/cc"
    if not cc.exists(): cc.symlink_to("gcc")
    files = []
    for path in sorted(output.rglob("*")):
        relative = path.relative_to(output).as_posix()
        if path.is_symlink(): files.append({"path":relative,"type":"symlink","target":os.readlink(path)})
        elif path.is_file(): files.append({"path":relative,"type":"file","bytes":path.stat().st_size,"sha256":sha(path)})
    evidence = {"schema":"mc.host-compiler-layer/1","status":"passed","packages":packages,
                "file_count":len(files),"files":files}
    descriptor = os.open(args.evidence, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(canonical(evidence) + b"\n"); stream.flush(); os.fsync(stream.fileno())
    print(canonical({"status":"passed","packages":len(packages),"files":len(files)}).decode())


if __name__ == "__main__": main()
