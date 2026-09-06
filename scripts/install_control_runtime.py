#!/usr/bin/env python3
"""Create the fixed pure-Python Server control runtime without ensurepip."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import venv
import zipfile


WHEELS = {
    "redis-5.3.1-py3-none-any.whl": "dc1909bd24669cc31b5f67a039700b16ec30571096c5f1f0d9d2324bff31af97",
    "pyjwt-2.12.1-py3-none-any.whl": "28ca37c070cad8ba8cd9790cd940535d40274d22f80ab87f3ac6a713e6e8454c",
    "async_timeout-5.0.1-py3-none-any.whl": "39e3809566ff85354557ec2398b55e096c8364bacac9405a7a1fa429e77fe76c",
    "typing_extensions-4.16.0-py3-none-any.whl": "481caa481374e813c1b176ada14e97f1f67a4539ce9cfeb3f350d78d6370c2e8",
}


def sha(path: Path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheels", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    args = parser.parse_args()
    wheels, target = args.wheels.absolute(), args.target.absolute()
    if target.exists() or not wheels.is_dir() or any(path.is_symlink() for path in (wheels, *wheels.parents)):
        raise ValueError("control_runtime_path_invalid")
    selected = []
    for name, expected in WHEELS.items():
        path = wheels / name
        if not path.is_file() or path.is_symlink() or sha(path) != expected:
            raise ValueError("control_runtime_wheel_changed")
        selected.append(path)
    venv.EnvBuilder(with_pip=False, symlinks=True).create(target)
    sites = list((target / "lib").glob("python*/site-packages"))
    if len(sites) != 1:
        raise ValueError("control_runtime_site_packages_invalid")
    site, seen = sites[0], set()
    for wheel in selected:
        with zipfile.ZipFile(wheel) as archive:
            for info in archive.infolist():
                path = PurePosixPath(info.filename)
                if (path.is_absolute() or not path.parts or ".." in path.parts or "\\" in info.filename
                        or stat.S_IFMT(info.external_attr >> 16) == stat.S_IFLNK):
                    raise ValueError("control_runtime_wheel_path_invalid")
                if info.is_dir():
                    continue
                destination = site.joinpath(*path.parts)
                relative = destination.relative_to(site).as_posix()
                if relative in seen or destination.exists():
                    raise ValueError("control_runtime_wheel_overlap")
                seen.add(relative); destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, destination.open("xb") as output:
                    for block in iter(lambda: source.read(1024 * 1024), b""):
                        output.write(block)
                os.chmod(destination, ((info.external_attr >> 16) & 0o777) or 0o644)
    python = target / "bin/python"
    subprocess.run([python, "-B", "-c",
        "import importlib.metadata as m,redis,jwt;"
        "assert redis.__version__=='5.3.1' and jwt.__version__=='2.12.1';"
        "assert m.version('async-timeout')=='5.0.1' and m.version('typing_extensions')=='4.16.0'"],
        check=True, stdin=subprocess.DEVNULL)
    evidence = {"schema": "mc.control-runtime/1", "python": subprocess.check_output(
        [python, "-c", "import platform;print(platform.python_version())"], text=True).strip(),
        "wheels": [{"filename": path.name, "sha256": WHEELS[path.name]} for path in selected]}
    descriptor = os.open(target / "installation-evidence.json", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(evidence, stream, sort_keys=True, separators=(",", ":")); stream.write("\n")
        stream.flush(); os.fsync(stream.fileno())
    print(json.dumps(evidence, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
