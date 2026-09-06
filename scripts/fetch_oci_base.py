#!/usr/bin/env python3
"""Fetch and freeze the fixed Ubuntu OCI base closure for offline assembly."""
from __future__ import annotations

import argparse
from pathlib import Path

from assemble_oci_images import Registry, canonical, exclusive, safe_root


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--maximum-download-bytes", type=int, default=1024**3)
    args = parser.parse_args()
    output = safe_root(args.output, existing=False)
    if output.exists():
        raise ValueError("oci_base_output_not_exclusive")
    output.mkdir(mode=0o700)
    base = Registry(output / "registry", args.maximum_download_bytes).ubuntu()
    closure = {"schema": "mc.oci-base-closure/1",
               "manifest_digest": base["manifest_digest"],
               "config_digest": base["config_digest"],
               "layers": [{key: row[key] for key in ("mediaType", "digest", "size")}
                          for row in base["layers"]],
               "diff_ids": base["diff_ids"]}
    exclusive(output / "closure.json", canonical(closure) + b"\n")
    print(canonical(dict(closure, downloaded_bytes=base["downloaded_bytes"])).decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
