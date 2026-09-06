#!/usr/bin/env python3
"""Run one bounded video adapter request and retain the native traceback.

This operator-only tool is intended for an isolated diagnostic container.  It
does not use Redis or the Server database and never turns a diagnostic result
into production task state.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import threading
import traceback
from pathlib import Path

from mediacenter.video_worker_cli import ADAPTERS


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bootstrap", type=Path, default=Path("/mc-bootstrap.json"))
    parser.add_argument("--asset-id", required=True)
    parser.add_argument("--media-type", default="image/png")
    parser.add_argument("--width", type=int, required=True)
    parser.add_argument("--height", type=int, required=True)
    parser.add_argument("--frames", type=int, required=True)
    parser.add_argument("--steps", type=int, required=True)
    parser.add_argument("--fps", type=int, required=True)
    parser.add_argument("--guidance-scale", type=float, required=True)
    args = parser.parse_args()

    value = json.loads(args.bootstrap.read_text(encoding="utf-8"))
    model = value["binding"]["model_key"]
    adapter_id, module, name, _minimum, _maximum, needs_inputs = ADAPTERS[model]
    if adapter_id != value["adapter_id"]:
        raise ValueError("bootstrap_adapter_mismatch")
    options = {
        "binding": value["binding"],
        "asset_bindings": value["asset_bindings"],
        "outputs": "/mc-outputs",
    }
    if needs_inputs:
        options["inputs"] = "/mc-inputs"
    imported = __import__(module, fromlist=[name])
    adapter = getattr(imported, name)(**options)
    input_path = Path("/mc-inputs") / (args.asset_id + ".png")
    revision = hashlib.sha256(input_path.read_bytes()).hexdigest()
    request = {
        "message_id": "cmd_diagnostic",
        "task_id": "task_diagnostic",
        "attempt_id": "attempt_diagnostic",
        "instance_id": value["instance_id"],
        "worker_epoch": value["worker_epoch"],
        "payload": {
            "model_key": model,
            "operation": "video.generate",
            "parameters": {
                "prompt": "The colored square gently drifts to the right.",
                "negative_prompt": "",
                "width": args.width,
                "height": args.height,
                "num_frames": args.frames,
                "steps": args.steps,
                "fps": args.fps,
                "guidance_scale": args.guidance_scale,
                "seed": 42,
            },
            "inputs": [{"asset_id": args.asset_id, "revision": revision,
                        "media_type": args.media_type}],
            "loras": [],
        },
    }
    result = {"schema": "mc.video-execute-diagnostic/1", "model": model, "progress": []}
    try:
        adapter.load(value["binding"])
        result["load"] = "passed"
        result["manifest"] = adapter.execute(
            request, result["progress"].append, threading.Event())
        result["execute"] = "passed"
    except BaseException as error:
        result.update(execute="failed", error_type=type(error).__name__,
                      error=str(error), traceback=traceback.format_exc())
    finally:
        try:
            adapter.unload()
            result["unload"] = "passed"
        except BaseException as error:
            result.update(unload="failed", unload_error_type=type(error).__name__,
                          unload_error=str(error), unload_traceback=traceback.format_exc())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("execute") == "passed" and result.get("unload") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
