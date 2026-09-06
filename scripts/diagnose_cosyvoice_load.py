#!/usr/bin/env python3
"""Bounded retained-container diagnostic for the CosyVoice load boundary."""
from __future__ import annotations

import json
import traceback

from mediacenter.audio_worker_cli import read_bootstrap
from mediacenter.adapters.cosyvoice import CosyVoiceAdapter


def main() -> int:
    value, _identity, _evidence = read_bootstrap()
    adapter = CosyVoiceAdapter(
        binding=value["binding"],
        asset_bindings=value["asset_bindings"],
        outputs=value["outputs"],
        source_root="/opt/cosyvoice",
    )
    result = {"schema": "mc.cosyvoice-load-diagnostic/1"}
    try:
        adapter.load(value["binding"])
        result["load"] = "passed"
    except BaseException as error:
        result.update(
            load="failed",
            error_type=type(error).__name__,
            error=str(error),
            traceback=traceback.format_exc(),
        )
    try:
        adapter.unload()
        result["unload"] = "passed"
    except BaseException as error:
        result.update(
            unload="failed",
            unload_error_type=type(error).__name__,
            unload_error=str(error),
            unload_traceback=traceback.format_exc(),
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("load") == "passed" and result.get("unload") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
