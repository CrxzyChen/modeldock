#!/usr/bin/env python3
"""Run the complete Linux suite across its control and media dependency domains."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import time


MEDIA_MODULES = (
    "tests.test_cosyvoice_container",
    "tests.test_music_adapter",
    "tests.test_runtime_provisioning",
    "tests.test_video_model_adapters",
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--control-python", type=Path, required=True)
    parser.add_argument("--media-python", type=Path, required=True)
    parser.add_argument("--control-site", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.absolute().resolve(strict=True)
    paths = [args.control_python, args.media_python, args.control_site]
    if any(not path.absolute().resolve(strict=True).exists() for path in paths):
        raise ValueError("candidate_suite_runtime_missing")
    media_names = {name.rsplit(".", 1)[-1] + ".py" for name in MEDIA_MODULES}
    control = ["tests." + path.stem for path in sorted((root / "tests").glob("test_*.py"))
               if path.name not in media_names]
    if not control or len(set(control)) != len(control):
        raise ValueError("candidate_suite_modules_invalid")
    started = time.monotonic()
    environment = dict(os.environ, PYTHONPATH=str(root))
    first = subprocess.run([str(args.control_python), "-B", "-m", "unittest", *control, "-v"],
                           cwd=root, env=environment, stdin=subprocess.DEVNULL)
    if first.returncode:
        return first.returncode
    environment["PYTHONPATH"] = str(root) + os.pathsep + str(args.control_site)
    second = subprocess.run([str(args.media_python), "-B", "-m", "unittest", *MEDIA_MODULES, "-v"],
                            cwd=root, env=environment, stdin=subprocess.DEVNULL)
    if second.returncode:
        return second.returncode
    result = {"schema": "mc.linux-candidate-suite/1", "status": "passed",
              "control_modules": len(control), "media_modules": len(MEDIA_MODULES),
              "elapsed_seconds": round(time.monotonic() - started, 3)}
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
