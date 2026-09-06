"""Run the bounded, authenticated 14-model production acceptance matrix.

The tool exercises only MediaCenter's public control API.  It never talks to
Docker, Redis, a Worker, or a GPU directly.  Every model is cold-loaded, used
twice without changing its runtime claim, and unloaded before the next model.
A representative cancellation is followed by a real recovery generation.
Evidence is checkpointed after every state transition so an interruption does
not turn an incomplete run into a pass.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import stat
import struct
import time
from urllib.parse import quote, urlsplit
import zlib


MODELS = [
    {"instance": "musicgen-small", "service": "music", "prompt": "A calm analog synth chord with a soft pulse.",
     "options": {"duration_seconds": 1.0, "guidance_scale": 1.0, "temperature": 1.0, "seed": 42}},
    {"instance": "sdxl-base-1.0", "service": "image", "prompt": "A red cube on a neutral studio table.",
     "options": {"width": 512, "height": 512, "steps": 1, "guidance_scale": 0.0, "seed": 42}},
    {"instance": "realesrgan-x2plus", "service": "image", "prompt": "AI upscale acceptance image.",
     "options": {}, "input": True},
    {"instance": "realesrgan-x4plus", "service": "image", "prompt": "AI upscale acceptance image.",
     "options": {}, "input": True},
    {"instance": "realesrgan-x4plus-anime-6b", "service": "image", "prompt": "Anime AI upscale acceptance image.",
     "options": {}, "input": True},
    {"instance": "z-image-turbo", "service": "image", "prompt": "A small blue ceramic bird on white paper.",
     "options": {"width": 512, "height": 512, "steps": 1, "guidance_scale": 0.0, "seed": 42}},
    {"instance": "qwen-image-2512", "service": "image", "prompt": "A clean poster containing the text MC 026.",
     "options": {"width": 1328, "height": 1328, "steps": 1, "true_cfg_scale": 1.0, "seed": 42}},
    {"instance": "illustrious-xl-v2.0", "service": "image", "prompt": "one character, simple background, clean line art",
     "options": {"width": 512, "height": 512, "steps": 1, "guidance_scale": 0.0, "seed": 42}},
    {"instance": "cosyvoice2-0.5b", "service": "speech", "prompt": "MediaCenter container acceptance passed.",
     "options": {"speed": 1.0}},
    {"instance": "minimax-h3-ref2va", "service": "video", "prompt": "<Picture 1> slowly moves closer while the camera remains stable.",
     "options": {"width": 512, "height": 320, "num_frames": 124, "steps": 2, "seed": 42}, "input": True},
    {"instance": "ltx-2.3", "service": "video", "prompt": "A paper boat moves slowly across a still pond, with quiet ambient sound.",
     "options": {"width": 512, "height": 320, "num_frames": 17, "fps": 8, "steps": 2,
                 "guidance_scale": 1.0, "stg_scale": 0.0, "modality_scale": 1.0, "seed": 42}},
    {"instance": "wan2.2-i2v", "service": "video", "prompt": "The colored square gently drifts to the right.",
     "options": {"width": 640, "height": 352, "num_frames": 9, "fps": 8, "steps": 1,
                 "guidance_scale": 0.0, "seed": 42}, "input": True},
    {"instance": "hunyuanvideo-1.5", "service": "video", "prompt": "A white paper kite floats in a clear sky, static camera.",
     "options": {"width": 640, "height": 352, "num_frames": 17, "fps": 8, "steps": 1, "seed": 42}},
    {"instance": "wan2.1-t2v-1.3b", "service": "video", "prompt": "A red ball rolls slowly across a plain floor, static camera.",
     "options": {"width": 480, "height": 320, "num_frames": 9, "fps": 8, "steps": 1,
                 "guidance_scale": 0.0, "seed": 42}},
]


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def require(condition, code):
    if not condition:
        error = RuntimeError(code)
        error.code = code
        raise error


def private_dir(path: Path):
    path.mkdir(mode=0o700, parents=True, exist_ok=False)
    if os.name == "posix":
        os.chmod(path, 0o700)
    return path


def checkpoint(root: Path, name: str, value):
    target = root / name
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(target, flags, 0o600)
    try:
        os.write(descriptor, canonical(value))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def png_fixture():
    width = height = 64
    rows = []
    for y in range(height):
        rows.append(b"\x00" + b"".join(bytes((x * 4, y * 4, 128)) for x in range(width)))
    raw = b"".join(rows)
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


class Client:
    def __init__(self, server_url, secret, response_limit):
        parsed = urlsplit(server_url)
        require(parsed.scheme in ("http", "https") and parsed.hostname and not parsed.username and not parsed.password,
                "server_url_invalid")
        require(parsed.path in ("", "/") and not parsed.query and not parsed.fragment, "server_url_invalid")
        self.target, self.secret, self.response_limit = parsed, secret, response_limit

    def request(self, method, path, body=None, *, key=None, content_type="application/json", statuses=(200, 201, 202)):
        require(path.startswith("/api/v1/") and ".." not in path and "\r" not in path and "\n" not in path,
                "api_path_invalid")
        factory = http.client.HTTPSConnection if self.target.scheme == "https" else http.client.HTTPConnection
        connection = factory(self.target.hostname, self.target.port, timeout=30)
        headers = {"X-API-Key": self.secret, "Accept": "application/json"}
        if key:
            headers["Idempotency-Key"] = key
        if body is None:
            data = None
        elif content_type == "application/json":
            data = canonical(body)
        else:
            data = body
        if data is not None:
            headers["Content-Type"] = content_type
            headers["Content-Length"] = str(len(data))
        try:
            connection.request(method, path, data, headers)
            response = connection.getresponse()
            raw = response.read(self.response_limit + 1)
            require(response.status in statuses, "api_http_%s_%s" % (response.status, raw[:256].decode(errors="replace")))
            require(len(raw) <= self.response_limit, "api_response_too_large")
            return json.loads(raw) if raw else None
        finally:
            connection.close()


def wait_validation(client, record, deadline):
    while record["state"] == "pending":
        require(time.monotonic() < deadline, "validation_timeout")
        time.sleep(1)
        record = client.request("GET", "/api/v1/runtime-validations/" + record["validation_id"])
    require(record["state"] == "passed", "validation_" + record["state"] + "_" + str(record.get("error_code")))
    return record


def wait_unloaded(client, instance, deadline):
    while True:
        target = client.request("GET", "/api/v1/deployments/%s/validation-target" % instance)
        if target["claim_id"] is None:
            return target
        require(time.monotonic() < deadline, "unload_timeout")
        time.sleep(1)


def task_for(row, asset_id):
    return {"service": row["service"], "model": row["instance"], "prompt": row["prompt"],
            "options": row["options"], "inputs": [asset_id] if row.get("input") else []}


def configure_residency(client, instance, target, residency, idle_seconds):
    policy = target["policy"]
    return client.request("POST", "/api/v1/deployments/%s/policy" % instance, {
        "version": target["policy_version"], "gpu_uuids": policy["gpus"],
        "sharing_mode": policy["sharing_mode"], "residency": residency,
        "external_reserve_mib": policy["external_reserve_mib"],
        "idle_minutes": idle_seconds // 60,
        "restart_recovery": target["restart_recovery"],
    })


def policy_record(instance, target):
    policy = target["policy"]
    return {"instance": instance, "gpu_uuids": policy["gpus"],
            "sharing_mode": policy["sharing_mode"], "residency": policy["residency"],
            "external_reserve_mib": policy["external_reserve_mib"],
            "idle_minutes": policy["idle_seconds"] // 60,
            "restart_recovery": target["restart_recovery"]}


def restore_modified_policies(client, root, timeout_seconds):
    restored = []
    for original_path in sorted(root.glob("*-policy-original.json")):
        marker = root / original_path.name.replace("-policy-original.json", "-policy-restored.json")
        if marker.exists():
            continue
        original = json.loads(original_path.read_text(encoding="utf-8"))
        instance = original["instance"]
        target = client.request("GET", "/api/v1/deployments/%s/validation-target" % instance)
        if target["claim_id"] is not None:
            client.request("POST", "/api/v1/deployments/%s/unload" % instance, {})
            target = wait_unloaded(client, instance, time.monotonic() + timeout_seconds)
        client.request("POST", "/api/v1/deployments/%s/policy" % instance, {
            "version": target["policy_version"], "gpu_uuids": original["gpu_uuids"],
            "sharing_mode": original["sharing_mode"], "residency": original["residency"],
            "external_reserve_mib": original["external_reserve_mib"],
            "idle_minutes": original["idle_minutes"],
            "restart_recovery": original["restart_recovery"],
        })
        checkpoint(root, marker.name, {"instance": instance, "status": "restored"})
        restored.append(instance)
    return restored


def cancel_pending_validations(client, root):
    canceled = []
    for reference in sorted(root.glob("*-validation.json")):
        identifier = json.loads(reference.read_text(encoding="utf-8"))["validation_id"]
        record = client.request("GET", "/api/v1/runtime-validations/" + identifier)
        if record["state"] != "pending":
            continue
        if record.get("task_id"):
            task = client.request("GET", "/api/v1/tasks/" + record["task_id"])
            if task["status"] not in ("succeeded", "failed", "canceled", "interrupted"):
                client.request("POST", "/api/v1/tasks/%s/cancel" % task["id"], {})
        else:
            client.request("POST", "/api/v1/runtime-validations/%s/cancel" % identifier,
                           {"version": record["version"]})
        canceled.append(identifier)
    return canceled


def run(args, client, root):
    started = time.monotonic()
    overall_deadline = started + args.total_timeout
    fixture = png_fixture()
    asset = client.request("POST", "/api/v1/assets?filename=" + quote("mc-acceptance-64.png"), fixture,
                           content_type="image/png")
    require(asset["sha256"] == hashlib.sha256(fixture).hexdigest(), "fixture_asset_hash_changed")
    checkpoint(root, "00-fixture.json", {"asset_id": asset["id"], "sha256": asset["sha256"],
                                         "byte_size": asset["byte_size"]})
    results = []
    for ordinal, row in enumerate([] if args.cancellation_only else MODELS, 1):
        instance = row["instance"]
        model_started = time.monotonic()
        target = client.request("GET", "/api/v1/deployments/%s/validation-target" % instance)
        if target["claim_id"] is not None:
            client.request("POST", "/api/v1/deployments/%s/unload" % instance, {})
            target = wait_unloaded(client, instance, min(overall_deadline, time.monotonic() + args.model_timeout))
        original_residency = target["policy"]["residency"]
        original_idle_seconds = target["policy"]["idle_seconds"]
        # The production default is intentionally on-demand.  This matrix
        # temporarily selects a bounded idle policy so the two generations
        # actually verify warm reuse, then restores the operator's setting.
        if original_residency != "idle" or original_idle_seconds < 60:
            checkpoint(root, "%02d-%s-policy-original.json" % (ordinal, instance),
                       policy_record(instance, target))
            configure_residency(client, instance, target, "idle", 3600)
            target = client.request("GET", "/api/v1/deployments/%s/validation-target" % instance)
        cold = client.request("POST", "/api/v1/deployments/%s/validate" % instance,
                              {"kind": "env_checked", "binding_digest": target["binding_digest"],
                               "version": target["policy_version"], "timeout_seconds": args.model_timeout},
                              key="%s-%02d-cold" % (args.run_id, ordinal))
        checkpoint(root, "%02d-%s-cold-validation.json" % (ordinal, instance),
                   {"validation_id": cold["validation_id"]})
        cold = wait_validation(client, cold, min(overall_deadline, time.monotonic() + args.model_timeout + 60))
        loaded_target = client.request("GET", "/api/v1/deployments/%s/validation-target" % instance)
        require(loaded_target["claim_id"] == cold["claim_id"] and loaded_target["claim_id"] is not None,
                "cold_claim_not_resident")
        generations = []
        for generation in (1, 2):
            task = task_for(row, asset["id"])
            record = client.request("POST", "/api/v1/deployments/%s/validate" % instance,
                                    {"kind": "generated_tested", "binding_digest": target["binding_digest"],
                                     "task": task, "timeout_seconds": args.model_timeout},
                                    key="%s-%02d-g%d" % (args.run_id, ordinal, generation))
            checkpoint(root, "%02d-%s-g%d-validation.json" % (ordinal, instance, generation),
                       {"validation_id": record["validation_id"]})
            done = wait_validation(client, record, min(overall_deadline, time.monotonic() + args.model_timeout + 60))
            require(done["claim_id"] == cold["claim_id"] and done["epoch"] == cold["epoch"], "warm_claim_changed")
            completed = client.request("GET", "/api/v1/tasks/" + done["task_id"])
            require(completed["status"] == "succeeded" and completed["current_attempt_id"] == done["attempt_id"],
                    "generated_task_changed")
            require(completed.get("output") and re.fullmatch("[0-9a-f]{64}", completed["output"]["sha256"]),
                    "generated_artifact_missing")
            generations.append({"validation_id": done["validation_id"], "task_id": done["task_id"],
                                "attempt_id": done["attempt_id"], "claim_id": done["claim_id"],
                                "epoch": done["epoch"], "artifact": completed["output"]})
        client.request("POST", "/api/v1/deployments/%s/unload" % instance, {})
        target = wait_unloaded(client, instance, min(overall_deadline, time.monotonic() + args.model_timeout))
        if original_residency != "idle" or original_idle_seconds < 60:
            configure_residency(client, instance, target, original_residency, original_idle_seconds)
            checkpoint(root, "%02d-%s-policy-restored.json" % (ordinal, instance),
                       {"instance": instance, "status": "restored"})
        entry = {"instance": instance, "service": row["service"], "status": "passed",
                 "cold_validation_id": cold["validation_id"], "cold_claim_id": cold["claim_id"],
                 "cold_epoch": cold["epoch"], "generations": generations,
                 "seconds": round(time.monotonic() - model_started, 3)}
        results.append(entry)
        checkpoint(root, "%02d-%s.json" % (ordinal, instance), entry)
    # Wan 2.1 is small enough to make cancellation deterministic without hiding
    # recovery behind another exceptionally long H3 load.
    row = next(item for item in MODELS if item["instance"] == "wan2.1-t2v-1.3b")
    target = client.request("GET", "/api/v1/deployments/wan2.1-t2v-1.3b/validation-target")
    cancel_original_residency = target["policy"]["residency"]
    cancel_original_idle_seconds = target["policy"]["idle_seconds"]
    if cancel_original_residency != "idle" or cancel_original_idle_seconds < 60:
        checkpoint(root, "15-wan2.1-t2v-1.3b-policy-original.json",
                   policy_record("wan2.1-t2v-1.3b", target))
        configure_residency(client, "wan2.1-t2v-1.3b", target, "idle", 3600)
        target = client.request("GET", "/api/v1/deployments/wan2.1-t2v-1.3b/validation-target")
    cold = wait_validation(client, client.request("POST", "/api/v1/deployments/wan2.1-t2v-1.3b/validate",
        {"kind": "env_checked", "binding_digest": target["binding_digest"], "version": target["policy_version"],
         "timeout_seconds": args.model_timeout}, key=args.run_id + "-cancel-load"),
        min(overall_deadline, time.monotonic() + args.model_timeout + 60))
    cancel_task = task_for({**row, "options": {**row["options"], "num_frames": 81, "steps": 50}}, asset["id"])
    pending = client.request("POST", "/api/v1/deployments/wan2.1-t2v-1.3b/validate",
        {"kind": "generated_tested", "binding_digest": target["binding_digest"], "task": cancel_task,
         "timeout_seconds": args.model_timeout}, key=args.run_id + "-cancel-task")
    cancel_deadline = min(overall_deadline, time.monotonic() + args.model_timeout)
    while True:
        task = client.request("GET", "/api/v1/tasks/" + pending["task_id"])
        if task["status"] == "running":
            break
        require(task["status"] not in ("succeeded", "failed", "canceled", "interrupted"), "cancel_not_observed_running")
        require(time.monotonic() < cancel_deadline, "cancel_start_timeout")
        time.sleep(.1)
    attempt_id = task["current_attempt_id"]
    client.request("POST", "/api/v1/tasks/%s/cancel" % task["id"], {})
    while True:
        task = client.request("GET", "/api/v1/tasks/" + task["id"])
        require(task["current_attempt_id"] == attempt_id, "cancel_attempt_changed")
        if task["status"] == "canceled" and task.get("attempt", {}).get("exit_confirmed"):
            break
        require(time.monotonic() < cancel_deadline, "cancel_exit_timeout")
        time.sleep(.1)
    recovery_task = task_for(row, asset["id"])
    recovery = wait_validation(client, client.request("POST", "/api/v1/deployments/wan2.1-t2v-1.3b/validate",
        {"kind": "generated_tested", "binding_digest": target["binding_digest"], "task": recovery_task,
         "timeout_seconds": args.model_timeout}, key=args.run_id + "-post-cancel"),
        min(overall_deadline, time.monotonic() + args.model_timeout + 60))
    require(recovery["claim_id"] == cold["claim_id"] and recovery["epoch"] == cold["epoch"], "recovery_claim_changed")
    recovered_task = client.request("GET", "/api/v1/tasks/" + recovery["task_id"])
    require(recovered_task["status"] == "succeeded", "post_cancel_recovery_failed")
    client.request("POST", "/api/v1/deployments/wan2.1-t2v-1.3b/unload", {})
    target = wait_unloaded(client, "wan2.1-t2v-1.3b", min(overall_deadline, time.monotonic() + args.model_timeout))
    if cancel_original_residency != "idle" or cancel_original_idle_seconds < 60:
        configure_residency(client, "wan2.1-t2v-1.3b", target,
                            cancel_original_residency, cancel_original_idle_seconds)
        checkpoint(root, "15-wan2.1-t2v-1.3b-policy-restored.json",
                   {"instance": "wan2.1-t2v-1.3b", "status": "restored"})
    cancellation = {"status": "passed", "model": "wan2.1-t2v-1.3b", "canceled_task_id": task["id"],
                    "canceled_attempt_id": attempt_id, "exit_evidence": task["attempt"]["exit_evidence"],
                    "recovery_task_id": recovery["task_id"], "claim_id": cold["claim_id"], "epoch": cold["epoch"]}
    checkpoint(root, "15-cancellation-recovery.json", cancellation)
    result = {"schema": 1, "status": "passed", "run_id": args.run_id, "model_count": len(results),
              "models": results, "cancellation_recovery": cancellation,
              "seconds": round(time.monotonic() - started, 3)}
    checkpoint(root, "result.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-url", required=True)
    parser.add_argument("--api-key-file", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--model-timeout", type=int, default=3600)
    parser.add_argument("--total-timeout", type=int, default=43200)
    parser.add_argument("--max-response-bytes", type=int, default=4 * 1024 * 1024)
    parser.add_argument("--cancellation-only", action="store_true",
                        help="Run only the bounded Wan2.1 cancellation and same-claim recovery gate.")
    args = parser.parse_args(argv)
    require(re.fullmatch("[A-Za-z0-9_.-]{1,96}", args.run_id) and ".." not in args.run_id, "run_id_invalid")
    require(10 <= args.model_timeout <= 3600 and 600 <= args.total_timeout <= 86400, "timeout_invalid")
    info = args.api_key_file.stat()
    require(os.name != "posix" or info.st_uid == os.getuid() and not stat.S_IMODE(info.st_mode) & 0o077,
            "api_key_not_private")
    secret = args.api_key_file.read_text(encoding="utf-8").strip()
    require(secret and "\n" not in secret and "\r" not in secret, "api_key_invalid")
    root = private_dir(args.evidence)
    checkpoint(root, "intent.json", {"schema": 1, "run_id": args.run_id, "status": "running",
                                     "models": ([] if args.cancellation_only else
                                                [row["instance"] for row in MODELS]),
                                     "cancellation_only": args.cancellation_only,
                                     "model_timeout": args.model_timeout, "total_timeout": args.total_timeout})
    client = Client(args.server_url, secret, args.max_response_bytes)
    try:
        result = run(args, client, root)
        print(json.dumps({"status": result["status"], "model_count": result["model_count"],
                          "seconds": result["seconds"]}, sort_keys=True))
        return 0
    except BaseException as error:
        try:
            canceled = cancel_pending_validations(client, root)
            restored = restore_modified_policies(client, root, args.model_timeout)
            checkpoint(root, "cleanup.json", {"status": "passed", "canceled": canceled,
                                               "restored": restored})
        except BaseException as cleanup_error:
            checkpoint(root, "cleanup-failure.json", {"status": "failed",
                       "error": getattr(cleanup_error, "code", type(cleanup_error).__name__)})
        checkpoint(root, "failure.json", {"status": "failed_or_incomplete",
                                           "error": getattr(error, "code", type(error).__name__)})
        raise


if __name__ == "__main__":
    raise SystemExit(main())
