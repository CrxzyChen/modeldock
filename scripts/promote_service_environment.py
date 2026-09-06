#!/usr/bin/env python3
"""Create a new non-secret service environment revision with one runtime config."""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from prepare_service_environment import read_environment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--runtime-config", required=True)
    args = parser.parse_args()
    source = Path(args.source).resolve()
    output = Path(args.environment).resolve()
    runtime = Path(args.runtime_config).resolve()
    values = read_environment(source)
    if "MEDIACENTER_API_KEY" in values or not Path(values["MEDIACENTER_API_KEY_FILE"]).is_file():
        raise ValueError("source must use an existing API key file")
    if not runtime.is_file() or output.exists() or not output.parent.is_dir():
        raise ValueError("runtime must exist and output must be new")
    values["MEDIACENTER_RUNTIME_CONFIG"] = str(runtime)
    body = "".join(f"{key}={values[key]}\n" for key in sorted(values)).encode()
    fd = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(body); stream.flush(); os.fsync(stream.fileno())
    print(f"prepared {len(values)} non-secret environment assignments")


if __name__ == "__main__":
    main()
