"""UUID scheduling over the single durable reservation ledger.

There is no context-manager lease: process exit, Python finally and lock release
are not GPU-release evidence. Observations never identify a process by its name.
"""
from __future__ import annotations

import math
from .hardware import HardwareProbe
from .task_state import TaskStateError


class GPUBusyError(RuntimeError):
    pass


class GPULeaseScheduler:
    SYSTEM_RESERVE_MIB = 2048

    def __init__(self, gpu_indices, *, authority, gpu_uuids, hardware=None, memory_provider=None):
        indices = tuple(gpu_indices)
        if not indices or len(set(indices)) != len(indices) or any(type(i) is not int or i < 0 for i in indices):
            raise ValueError("explicit GPU index view required")
        if not isinstance(gpu_uuids, (list, tuple)) or len(set(gpu_uuids)) != len(gpu_uuids) or gpu_uuids and len(gpu_uuids) != len(indices):
            raise ValueError("explicit UUID pool required")
        if any(not isinstance(v, str) or not v.startswith("GPU-") for v in gpu_uuids):
            raise ValueError("explicit UUID pool required")
        self.authority = authority
        self.repository = authority.repository
        self.allowed_indices = indices
        self.allowed_uuids = tuple(gpu_uuids)
        self.hardware = hardware or HardwareProbe(indices)
        if memory_provider is None:
            from .resource_observer import linux_memory_state
            memory_provider = linux_memory_state
        self.memory_provider = memory_provider

    def _observed(self):
        snapshot = self.hardware.gpu_snapshot(timeout_seconds=0.5)
        if not snapshot["available"]:
            raise GPUBusyError("gpu_telemetry_unavailable")
        rows = snapshot["items"]
        if len({row["uuid"] for row in rows}) != len(rows):
            raise GPUBusyError("gpu_identity_ambiguous")
        return {row["uuid"]: row for row in rows}

    def capacity(self, policy, *, task=False):
        requested = set(policy["gpus"])
        if not requested or not requested <= set(self.allowed_uuids):
            raise GPUBusyError("gpu_outside_explicit_pool")
        memory = self.memory_provider()
        if not math.isfinite(memory.pressure_avg60) or memory.available_bytes < 32 * 1024 ** 3 or memory.pressure_avg60 > 5:
            raise GPUBusyError("host_memory_pressure")
        inventory = self._observed()
        margin = self.SYSTEM_RESERVE_MIB + policy["external_reserve_mib"]
        increment = policy["task_mib"] if task else policy["base_mib"]
        limits = {}
        for gpu in requested:
            item = inventory.get(gpu)
            if not item:
                raise GPUBusyError("gpu_telemetry_unavailable")
            used, total = item.get("memory_used_mib"), item.get("memory_total_mib")
            if type(used) is not int or type(total) is not int or not 0 <= used <= total:
                raise GPUBusyError("gpu_telemetry_unavailable")
            if total - used < increment + margin:
                raise GPUBusyError("gpu_capacity_unavailable")
            if not task and policy["sharing_mode"] == "exclusive" and item.get("processes"):
                raise GPUBusyError("gpu_exclusive_observed_occupancy")
            limits[gpu] = {"limit": total - margin, "free": max(0, total - used - margin)}
        return limits

    def runtime_capacity(self, policy):
        """Validate the configured GPU identity without reserving model memory.

        Starting a service creates its container and Worker only.  Model memory
        admission belongs to model.load, so an idle on-demand service does not
        pretend to consume its base budget.
        """
        requested = set(policy["gpus"])
        if not requested or not requested <= set(self.allowed_uuids):
            raise GPUBusyError("gpu_outside_explicit_pool")
        inventory = self._observed()
        limits = {}
        margin = self.SYSTEM_RESERVE_MIB + policy["external_reserve_mib"]
        for gpu in requested:
            item = inventory.get(gpu)
            total = item.get("memory_total_mib") if item else None
            if type(total) is not int or total <= margin:
                raise GPUBusyError("gpu_telemetry_unavailable")
            limits[gpu] = total - margin
        return limits

    def validate_budget(self, policy):
        """Reject a durable policy whose base + task ledger can never fit."""
        requested = set(policy["gpus"])
        if not requested or not requested <= set(self.allowed_uuids):
            raise GPUBusyError("gpu_outside_explicit_pool")
        inventory = self._observed()
        required = policy["base_mib"] + policy["task_mib"]
        for gpu in requested:
            item = inventory.get(gpu)
            total = item.get("memory_total_mib") if item else None
            if type(total) is not int or total <= 0:
                raise GPUBusyError("gpu_telemetry_unavailable")
            if required + self.SYSTEM_RESERVE_MIB + policy["external_reserve_mib"] > total:
                raise GPUBusyError("gpu_policy_unschedulable")
        return True

    def start_container(self, instance, package_identity):
        row = self.authority.get(instance)
        if row is None or row["policy"] is None:
            raise TaskStateError("instance_policy_required")
        # Container admission is independent of model-memory admission.
        policy = row["policy"]
        limits = self.runtime_capacity(policy)
        return self.authority.claim_container(instance, package_identity["epoch"],
            expected_version=row["version"], backend=policy["backend"], limits=limits,
            package_identity=package_identity)

    def materialize_container(self, instance, package_identity):
        """Claim the exact stopped container without opening task admission."""
        row = self.authority.get(instance)
        if row is None or row["policy"] is None:
            raise TaskStateError("instance_policy_required")
        policy = row["policy"]
        limits = self.runtime_capacity(policy)
        return self.authority.claim_container(
            instance, package_identity["epoch"], expected_version=row["version"],
            backend=policy["backend"], limits=limits,
            package_identity=package_identity, materialize_only=True)

    def load_model(self, instance):
        return self.authority.load_model(instance, observe_capacity=self.capacity)

    def dispatch(self, task):
        row = self.authority.get(task["model"])
        if not row or not row["claim"]:
            raise TaskStateError("instance_not_loaded")
        policy, claim = row["policy"], row["claim"]
        return self.authority.tasks.dispatch(task["id"], task["version"], task["model"],
            claim["epoch"], {gpu: policy["task_mib"] for gpu in policy["gpus"]}, observe_capacity=self.capacity)

    def inventory(self):
        rows = self._observed()
        return [{"index": rows[gpu]["index"] if gpu in rows else None, "uuid": gpu,
                 "assignable": gpu in rows} for gpu in self.allowed_uuids]

    def resource_snapshot(self, *, observation=None):
        if observation is None:
            try:
                observed = self._observed()
                error = None
            except GPUBusyError as exc:
                observed, error = {}, str(exc)
        elif observation.get("available"):
            observed = {item["uuid"]: item for item in observation.get("items", [])}
            error = None
        else:
            observed, error = {}, observation.get("error") or "gpu_telemetry_unavailable"
        with self.repository._connect() as db:
            reservations = db.execute("SELECT gpu_uuid,kind,SUM(mib) AS mib FROM task_reservations WHERE released=0 GROUP BY gpu_uuid,kind").fetchall()
            blocked = self.authority.legacy_unreconciled(db)
        totals = {}
        for row in reservations:
            totals.setdefault(row["gpu_uuid"], {})[row["kind"]] = row["mib"]
        result = []
        for view_index, gpu in zip(self.allowed_indices, self.allowed_uuids):
            item = observed.get(gpu, {})
            held = totals.get(gpu, {})
            total, used = item.get("memory_total_mib"), item.get("memory_used_mib")
            free = total - used if type(total) is int and type(used) is int else None
            available = free is not None and free > self.SYSTEM_RESERVE_MIB and not blocked
            result.append({**item, "index": item.get("index", view_index), "uuid": gpu,
                "assignable": True, "memory_free_mib": free,
                "system_reserve_mib": self.SYSTEM_RESERVE_MIB,
                "shared_capacity_mib": max(0, free - self.SYSTEM_RESERVE_MIB) if free is not None else None,
                "base_reserved_mib": held.get("base", 0), "task_reserved_mib": held.get("task", 0),
                "managed_task_active": bool(held.get("task")), "managed_model_warm": bool(held.get("base")),
                "available_for_new_task": available,
                "availability_reason": "legacy_execution_unreconciled" if blocked else "capacity_observed" if available else "capacity_unavailable",
                "processes": [{**process, "ownership": "unattributed"} for process in item.get("processes", [])]})
        return {"telemetry_available": error is None and all(gpu in observed for gpu in self.allowed_uuids),
                "telemetry_error": error, "gpus": result}
