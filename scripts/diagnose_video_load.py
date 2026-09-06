#!/usr/bin/env python3
"""Bounded retained-container diagnostic for one video model load boundary."""
from __future__ import annotations

import importlib
import json
import traceback

from mediacenter.video_worker_cli import ADAPTERS, read_bootstrap


def main() -> int:
    value, _identity, _evidence = read_bootstrap()
    model = value["binding"]["model_key"]
    _adapter_id, module, name, _minimum, _maximum, needs_inputs = ADAPTERS[model]
    options = dict(
        binding=value["binding"],
        asset_bindings=value["asset_bindings"],
        outputs=value["outputs"],
    )
    if needs_inputs:
        options["inputs"] = "/mc-inputs"
    adapter = getattr(importlib.import_module(module), name)(**options)
    result = {"schema": "mc.video-load-diagnostic/1", "model": model}
    try:
        adapter.load(value["binding"])
        result["load"] = "passed"
    except BaseException as error:
        result.update(load="failed", error_type=type(error).__name__, error=str(error),
                      traceback=traceback.format_exc())
    try:
        adapter.unload()
        result["unload"] = "passed"
    except BaseException as error:
        result.update(unload="failed", unload_error_type=type(error).__name__, unload_error=str(error),
                      unload_traceback=traceback.format_exc())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("load") == "passed" and result.get("unload") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
