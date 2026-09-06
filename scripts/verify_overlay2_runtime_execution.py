#!/usr/bin/env python3
"""Execute every imported production image as the unprivileged runtime user."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def write_new(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())


def verify(assembly_path):
    assembly_path = Path(assembly_path).resolve()
    assembly = json.loads(assembly_path.read_text(encoding="utf-8"))
    if assembly.get("status") != "complete":
        raise ValueError("complete assembly required")
    rows = []
    for image in sorted(assembly["images"], key=lambda value: value["id"]):
        if not image.get("entrypoint"):
            continue
        module = image["entrypoint"][-1]
        dependency_probe = ("import packaging.specifiers,setuptools;from transformers import PreTrainedModel;"
                            if image["id"] == "cosyvoice2-0.5b" else "")
        if image["id"] == "video-models":
            dependency_probe = ("import ctypes,ftfy,subprocess,wcwidth;"
                "r=subprocess.run(['/usr/bin/cc','-shared','-fPIC','-x','c','-','-o','/mc-triton-cache/mc-cc.so'],"
                "input=b'int mc_probe(void){return 0;}\\n',capture_output=True);"
                "assert r.returncode==0 and pathlib.Path('/mc-triton-cache/mc-cc.so').read_bytes()[:4]==b'\\x7fELF';"
                "assert ctypes.CDLL('/mc-triton-cache/mc-cc.so').mc_probe()==0;")
        probe = (
            "import importlib,os,pathlib;"
            "p=pathlib.Path('/opt/mediacenter/mediacenter');"
            "assert p.is_dir() and os.access(p,os.R_OK|os.X_OK);"
            f"importlib.import_module({module!r});"
            + dependency_probe +
            "print('runtime-execution-ok')"
        )
        command = [
            "docker", "run", "--rm", "--network", "none", "--read-only",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m",
            "--user", "1000:1000", "--cap-drop", "ALL", "--security-opt",
            "no-new-privileges", "--label", "mediacenter.owner=runtime-execution-verifier",
            "--entrypoint", "/opt/python/bin/python", image["config_digest"],
            "-B", "-c", probe,
        ]
        if image["id"] == "video-models":
            command[8:8] = ["--tmpfs", "/mc-triton-cache:rw,exec,nosuid,nodev,size=64m,mode=700,uid=1000,gid=1000"]
        completed = subprocess.run(command, capture_output=True, text=True, timeout=180)
        row = {"id": image["id"], "image_id": image["config_digest"],
               "module": module, "exit_code": completed.returncode,
               "stdout_sha256": hashlib.sha256(completed.stdout.encode()).hexdigest(),
               "stderr_sha256": hashlib.sha256(completed.stderr.encode()).hexdigest()}
        rows.append(row)
        if completed.returncode != 0 or completed.stdout.strip() != "runtime-execution-ok":
            raise RuntimeError(canonical(row))
    return {"schema": "mc.overlay2-runtime-execution/1", "status": "passed",
            "assembly": str(assembly_path), "image_count": len(rows), "images": rows}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--assembly", required=True)
    parser.add_argument("--evidence", required=True)
    args = parser.parse_args()
    result = verify(args.assembly)
    raw = (canonical(result) + "\n").encode()
    write_new(Path(args.evidence).resolve(), raw)
    print(raw.decode(), end="")


if __name__ == "__main__":
    main()
