#!/usr/bin/env python3
"""Bounded production SSE probe for the PH-7 operations checkpoint."""
from __future__ import annotations

import argparse
import http.client
import json
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlsplit


def read_event(response) -> dict[str, object]:
    event: dict[str, object] = {"name": None, "id": None, "data": None}
    data: list[str] = []
    while True:
        line = response.readline()
        if not line or line in {b"\n", b"\r\n"}:
            break
        text = line.decode("utf-8").rstrip("\r\n")
        if text.startswith("event: "):
            event["name"] = text[7:]
        elif text.startswith("id: "):
            event["id"] = text[4:]
        elif text.startswith("data: "):
            data.append(text[6:])
    if data:
        event["data"] = json.loads("\n".join(data))
    return event


def connection(parts, timeout=5.0):
    if parts.scheme != "http" or not parts.hostname or not parts.port:
        raise RuntimeError("invalid_probe_url")
    return http.client.HTTPConnection(parts.hostname, parts.port, timeout=timeout)


def open_sse(parts, key, last_event_id=None, *, timeout=5.0):
    client = connection(parts, timeout=timeout)
    headers = {"X-API-Key": key}
    if last_event_id:
        headers["Last-Event-ID"] = last_event_id
    client.request("GET", "/api/v1/events", headers=headers)
    response = client.getresponse()
    event = read_event(response) if response.status == 200 else None
    return client, response, event


def authenticated_overview(parts, key):
    client = connection(parts)
    started = time.monotonic()
    try:
        client.request("GET", "/api/v1/overview", headers={"X-API-Key": key})
        response = client.getresponse()
        response.read()
        return response.status, round((time.monotonic() - started) * 1000, 3)
    finally:
        client.close()


def storage_snapshot(database: Path) -> dict[str, object]:
    files = {}
    for path in (database, Path(str(database) + "-wal"), Path(str(database) + "-shm")):
        files[path.name] = path.stat().st_size if path.exists() else None
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as db:
        page_count = db.execute("PRAGMA page_count").fetchone()[0]
        freelist_count = db.execute("PRAGMA freelist_count").fetchone()[0]
    return {"files": files, "page_count": page_count,
            "freelist_count": freelist_count}


def idle_probe(parts, key, seconds: int, database: Path) -> dict[str, object]:
    before = storage_snapshot(database)
    client, response, hello = open_sse(parts, key, timeout=30.0)
    started = time.monotonic()
    heartbeat_frames = 0
    try:
        if response.status != 200 or not hello or hello["name"] != "hello":
            raise RuntimeError(f"sse_open_failed_{response.status}")
        deadline = started + seconds
        while time.monotonic() < deadline:
            line = response.readline()
            if not line:
                raise RuntimeError("sse_closed_during_idle_observation")
            if line.startswith(b": heartbeat "):
                heartbeat_frames += 1
    finally:
        response.close()
        client.close()
    after = storage_snapshot(database)
    elapsed = round(time.monotonic() - started, 3)
    stable = before == after
    return {"schema": "mc.ph7-sse-idle-probe/1",
            "status": "passed" if stable and elapsed >= seconds else "failed",
            "requested_seconds": seconds, "elapsed_seconds": elapsed,
            "heartbeat_frames": heartbeat_frames, "before": before,
            "after": after, "storage_growth_bytes": {
                name: ((after["files"][name] or 0) - (size or 0))
                for name, size in before["files"].items()},
            "page_count_growth": after["page_count"] - before["page_count"],
            "freelist_count_growth": after["freelist_count"] - before["freelist_count"]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--api-key-file", type=Path, required=True)
    parser.add_argument("--idle-seconds", type=int, default=0)
    parser.add_argument("--database", type=Path)
    args = parser.parse_args()
    parts = urlsplit(args.url)
    key = args.api_key_file.read_text(encoding="utf-8").strip()
    if args.idle_seconds:
        if not 1 <= args.idle_seconds <= 3600 or args.database is None:
            raise SystemExit("idle probe requires --database and 1..3600 seconds")
        result = idle_probe(parts, key, args.idle_seconds, args.database)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0 if result["status"] == "passed" else 1
    clients = []
    result: dict[str, object] = {"schema": "mc.ph7-sse-probe/1", "status": "failed"}
    try:
        hello_ids = []
        for _ in range(32):
            client, response, event = open_sse(parts, key)
            if response.status != 200 or not event or event["name"] != "hello":
                raise RuntimeError(f"sse_open_failed_{response.status}")
            clients.append((client, response))
            hello_ids.append(event["id"])
        overflow, denied, _ = open_sse(parts, key)
        denied.read()
        overflow.close()
        overview_status, overview_latency_ms = authenticated_overview(parts, key)
        result.update({
            "opened_clients": len(clients),
            "overflow_status": denied.status,
            "slow_client_overview_status": overview_status,
            "slow_client_overview_latency_ms": overview_latency_ms,
        })
        released_client, released_response = clients.pop()
        cursor = hello_ids[-1]
        released_response.close()
        released_client.close()
        replacement = None
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            candidate, response, event = open_sse(parts, key, cursor)
            if response.status == 200:
                replacement = (candidate, response, event)
                break
            response.read()
            candidate.close()
            time.sleep(0.5)
        if replacement is None:
            raise RuntimeError("sse_slot_not_recovered")
        clients.append((replacement[0], replacement[1]))
        hello = replacement[2]["data"] if replacement[2] else None
        data = hello.get("data") if isinstance(hello, dict) else None
        result.update({
            "status": "passed",
            "opened_clients": len(clients),
            "overflow_status": denied.status,
            "replacement_status": replacement[1].status,
            "cursor_reused": bool(replacement[2] and replacement[2]["id"] == cursor),
            "hello_contract": data,
            "slow_client_overview_status": overview_status,
            "slow_client_overview_latency_ms": overview_latency_ms,
        })
        required = (denied.status == 503 and replacement[1].status == 200
                    and overview_status == 200 and overview_latency_ms < 2000
                    and data == {"capacity": 1024, "max_event_bytes": 16384})
        result["status"] = "passed" if required else "failed"
    except Exception as exc:
        result["error_code"] = str(exc)
        result["opened_before_failure"] = len(clients)
    finally:
        for client, response in clients:
            response.close()
            client.close()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
