#!/usr/bin/env python3
"""Read-only verification of one explicitly pinned production Docker engine."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mediacenter.config import EngineConfig
from mediacenter.container_runtime import UnixEngine


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument("--engine-id", required=True)
    parser.add_argument("--api-version", required=True)
    parser.add_argument("--server-version", required=True)
    parser.add_argument("--cgroup-driver", required=True, choices=("cgroupfs", "systemd"))
    parser.add_argument("--image-store", required=True, choices=("containerd", "overlay2"))
    args = parser.parse_args()
    config = EngineConfig(args.socket, args.engine_id, "/sys/fs/cgroup", "/sys/fs/cgroup",
                          api_version=args.api_version, server_version=args.server_version,
                          cgroup_driver=args.cgroup_driver, image_store=args.image_store)
    result = UnixEngine(config).verify_engine()
    print(json.dumps({"schema": "mc.actual-engine-verification/1", "status": "passed",
                      "engine": {"id": args.engine_id, "api": args.api_version,
                                 "version": args.server_version, "cgroup_driver": args.cgroup_driver,
                                 "image_store": args.image_store}, "result": result},
                     sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
