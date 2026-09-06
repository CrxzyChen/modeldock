"""Shared JSON identity primitives. No database, controller or process imports.

The error name/status and canonical byte representation are the existing wire
contract; moving them here does not change task or Worker behavior.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


class TaskStateError(ValueError):
    def __init__(self, code: str, status: int = 409):
        self.code, self.status = code, status
        super().__init__(code)


def canonical(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise TaskStateError("invalid_task_json", 400) from None


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()
