#!/usr/bin/env python3
"""Run bounded real cold-load validation and unload through the public API."""
from __future__ import annotations

import argparse
import http.client
import json
import os
from pathlib import Path
import secrets
import time
from urllib.parse import urlsplit


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def write_new(path, value):
    raw = (canonical(value) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())


class Client:
    def __init__(self, base, key):
        parsed = urlsplit(base)
        if parsed.scheme != "http" or not parsed.hostname or parsed.path not in ("", "/"):
            raise ValueError("base_invalid")
        self.host, self.port, self.key = parsed.hostname, parsed.port or 80, key

    def request(self, method, path, body=None, idempotency=None):
        headers = {"X-API-Key": self.key, "Accept": "application/json"}
        raw = None
        if body is not None:
            raw = canonical(body).encode(); headers["Content-Type"] = "application/json"
        if idempotency: headers["Idempotency-Key"] = idempotency
        connection = http.client.HTTPConnection(self.host, self.port, timeout=30)
        try:
            connection.request(method, path, raw, headers)
            response = connection.getresponse(); data = response.read(1024 * 1024 + 1)
            if response.status not in (200, 201, 202) or len(data) > 1024 * 1024:
                raise RuntimeError(f"http_{response.status}")
            return json.loads(data)
        finally:
            connection.close()


def wait_validation(client, record, deadline):
    while True:
        current = client.request("GET", "/api/v1/runtime-validations/" + record["validation_id"])
        if current["state"] != "pending":
            if current["state"] != "passed": raise RuntimeError("validation_" + current["state"])
            return current
        if time.monotonic() >= deadline: raise TimeoutError("validation_timeout")
        time.sleep(1)


def wait_unloaded(client, model, deadline):
    while True:
        target = client.request("GET", f"/api/v1/deployments/{model}/validation-target")
        if target.get("claim_id") is None: return target
        if time.monotonic() >= deadline: raise TimeoutError("unload_timeout")
        time.sleep(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8787")
    parser.add_argument("--api-key-file", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--models", nargs="*")
    parser.add_argument("--model-timeout", type=int, default=3600)
    args = parser.parse_args()
    key = args.api_key_file.read_text(encoding="utf-8").strip()
    if not key or "\n" in key or "\r" in key: raise ValueError("api_key_invalid")
    client = Client(args.base, key)
    catalog = client.request("GET", "/api/v1/service-catalog")["items"]
    available = [item["deployment_id"] for item in catalog if item["state"] == "installed"]
    models = args.models or available
    if not models or len(models) != len(set(models)) or any(model not in available for model in models):
        raise ValueError("model_selection_invalid")
    run_id = "env-matrix-" + secrets.token_hex(8); rows = []
    result = {"schema": "mc.real-env-validation-matrix/1", "status": "in_progress",
              "run_id": run_id, "models": models, "results": rows}
    try:
        for ordinal, model in enumerate(models, 1):
            deadline = time.monotonic() + args.model_timeout
            initial = wait_unloaded(client, model, deadline)
            started = time.monotonic()
            record = client.request("POST", f"/api/v1/deployments/{model}/validate",
                {"kind":"env_checked", "binding_digest":initial["binding_digest"],
                 "version":initial["policy_version"], "timeout_seconds":args.model_timeout},
                f"{run_id}-{ordinal}-load")
            passed = wait_validation(client, record, deadline)
            loaded_seconds = round(time.monotonic() - started, 3)
            client.request("POST", f"/api/v1/deployments/{model}/unload", {})
            wait_unloaded(client, model, deadline)
            rows.append({"model":model, "validation_id":passed["validation_id"],
                         "claim_id":passed["claim_id"], "epoch":passed["epoch"],
                         "cold_load_seconds":loaded_seconds, "unloaded":True})
        result["status"] = "passed"
    except BaseException as error:
        result.update(status="failed", error=type(error).__name__, error_message=str(error))
        write_new(args.evidence, result)
        raise
    write_new(args.evidence, result)
    print(canonical({"status":"passed", "models":len(rows),
                     "elapsed_seconds":round(sum(row["cold_load_seconds"] for row in rows),3)}))


if __name__ == "__main__":
    main()
