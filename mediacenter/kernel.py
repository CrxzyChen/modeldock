"""Production Server composition of the one durable runtime authority."""
from __future__ import annotations

from .gpu_scheduler import GPULeaseScheduler
from .instance_policy import InstancePolicy
from .reconciler import Reconciler
from .task_state import TaskState


class DeploymentLifecycle(Reconciler):
    def __init__(self, repository, registry, artifact_root, *, gpu_indices, gpu_uuids=(),
                 hardware=None, memory_provider=None, server_id="mediacenter",
                 poll_seconds=0.5, model_assets=None, package_provider=None):
        authority = InstancePolicy(repository, TaskState(repository, server_id=server_id))
        scheduler = GPULeaseScheduler(gpu_indices, authority=authority, gpu_uuids=gpu_uuids,
                                      hardware=hardware, memory_provider=memory_provider)
        super().__init__(repository, registry, scheduler,
                         poll_seconds=poll_seconds, artifact_root=artifact_root, model_assets=model_assets,
                         package_provider=package_provider)
