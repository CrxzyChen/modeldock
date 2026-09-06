#!/usr/bin/env python3
"""Adopt the frozen 14 existing deployments through the public installer API."""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path


DEPLOYMENTS = {
    "musicgen-small": "musicgen-small",
    "sdxl-base-1.0": "sdxl-base-1.0",
    "realesrgan-x2plus": "realesrgan-x2plus",
    "realesrgan-x4plus": "realesrgan-x4plus",
    "realesrgan-x4plus-anime-6b": "realesrgan-x4plus-anime-6b",
    "z-image-turbo": "z-image-turbo",
    "qwen-image-2512": "qwen-image-2512",
    "illustrious-xl-v2.0": "illustrious-xl-v2.0",
    "cosyvoice2-0.5b": "cosyvoice2-0.5b",
    "minimax-h3-ref2va": "minimax-h3-ref2va",
    "ltx-2.3-distilled": "ltx-2.3",
    "wan2.2-i2v-a14b": "wan2.2-i2v",
    "hunyuanvideo-1.5-720p-t2v": "hunyuanvideo-1.5",
    "wan2.1-t2v-1.3b": "wan2.1-t2v-1.3b",
}


class Client:
    def __init__(self, base: str, key_file: Path):
        self.base = base.rstrip("/")
        self.key = key_file.read_text(encoding="utf-8").strip()
        if not self.key:
            raise ValueError("API key is empty")

    def request(self, path: str, body=None):
        raw = None if body is None else json.dumps(body, separators=(",", ":")).encode()
        request = urllib.request.Request(self.base + path, data=raw,
            method="GET" if raw is None else "POST",
            headers={"X-API-Key": self.key,
                     **({"Content-Type": "application/json"} if raw else {})})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)


def order(items: dict) -> list[str]:
    result, pending = [], set(items)
    while pending:
        ready = sorted(key for key in pending if all(
            item["recipe_key"] not in pending for item in items[key]["prerequisites"]))
        if not ready:
            raise ValueError("service prerequisite cycle")
        result.extend(ready); pending.difference_update(ready)
    return result


def checkpoint(path: Path, value: dict) -> None:
    pending = path.with_name(path.name + ".pending")
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    flags = os.O_CREAT | os.O_TRUNC | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(pending, flags, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    os.replace(pending, path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8787")
    parser.add_argument("--api-key-file", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--timeout", type=int, default=7200)
    args = parser.parse_args()
    if args.evidence.exists() or not args.evidence.parent.is_dir():
        raise ValueError("adoption evidence must be new")
    client = Client(args.base, args.api_key_file)
    status, catalog = client.request("/api/v1/service-catalog")
    items = {item["recipe_key"]: item for item in catalog.get("items", [])}
    if status != 200 or set(items) != set(DEPLOYMENTS):
        raise ValueError("Server catalog is not the frozen 14-model matrix")
    evidence = {"schema": "mc.existing-model-adoption/1", "status": "in_progress",
                "models": []}
    checkpoint(args.evidence, evidence)
    began = time.monotonic()
    for index, key in enumerate(order(items)):
        current = items[key]
        if current["state"] == "installed":
            evidence["models"].append({"recipe_key": key, "deployment_id": DEPLOYMENTS[key],
                                       "state": "already_installed"})
            continue
        gpus = [0, 1] if current["min_gpus"] == 2 else [index % 2]
        status, operation = client.request("/api/v1/service-installations", {
            "recipe_key": key, "deployment_id": DEPLOYMENTS[key], "gpu_indices": gpus,
            "license_accepted": True, "adopt_existing": True,
            "startup_policy": "manual", "gpu_sharing_mode": "exclusive"})
        if status != 202:
            raise RuntimeError(f"{key} adoption rejected: {status} {operation.get('error', {}).get('code')}")
        operation_id = operation["id"]
        while operation["state"] not in {"ready", "failed", "canceled"}:
            if time.monotonic() - began > args.timeout:
                raise TimeoutError("14-model adoption budget exceeded")
            time.sleep(2)
            status, operation = client.request("/api/v1/service-installations/" + operation_id)
            if status != 200:
                raise RuntimeError(f"{key} adoption status unavailable: {status}")
        record = {"recipe_key": key, "deployment_id": DEPLOYMENTS[key],
                  "operation_id": operation_id, "state": operation["state"],
                  "error_code": operation.get("error_code")}
        evidence["models"].append(record)
        checkpoint(args.evidence, evidence)
        if operation["state"] != "ready":
            evidence["status"] = "failed"
            checkpoint(args.evidence, evidence)
            raise RuntimeError(f"{key} adoption failed: {operation.get('error_code')}")
    status, final_catalog = client.request("/api/v1/service-catalog")
    states = {item["recipe_key"]: item["state"] for item in final_catalog.get("items", [])}
    evidence.update(status="passed", elapsed_seconds=round(time.monotonic() - began, 3),
                    installed_states=states)
    checkpoint(args.evidence, evidence)
    print(json.dumps({"status": evidence["status"], "models": len(evidence["models"]),
                      "elapsed_seconds": evidence["elapsed_seconds"]}, separators=(",", ":")))


if __name__ == "__main__":
    main()
