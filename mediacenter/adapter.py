"""Trusted in-process model adapter contract, instantiated only in inference.

Factory selection is deployment configuration, NEVER a field in an Envelope.
Adapters must synchronize their task work before reset returns successfully;
failure to restore clean adapter state quarantines the inference process.
"""
from __future__ import annotations

import importlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from .capabilities import validate_worker_capability, validate_worker_request


class Adapter(ABC):
    @abstractmethod
    def describe_capabilities(self) -> dict: ...

    def validate_request(self, request: dict) -> None:
        capability = self.describe_capabilities()
        validate_worker_capability(capability)
        payload = request["payload"]
        validate_worker_request(payload["model_key"], payload["operation"], payload["parameters"],
                                payload["inputs"], payload["loras"], capability=capability)

    @abstractmethod
    def load(self, binding: dict) -> None: ...

    @abstractmethod
    def execute(self, request: dict, progress, cancellation) -> dict: ...

    @abstractmethod
    def reset_task_state(self) -> None: ...

    @abstractmethod
    def unload(self) -> None: ...


@dataclass(frozen=True)
class AdapterFactory:
    module: str
    name: str
    options: dict = field(default_factory=dict, repr=False)

    def create(self) -> Adapter:
        # This object is built by trusted runtime wiring, not deserialized from
        # task input. Model modules/imports stay out of the control Supervisor.
        instance = getattr(importlib.import_module(self.module), self.name)(**self.options)
        if not isinstance(instance, Adapter):
            raise TypeError("invalid_adapter")
        validate_worker_capability(instance.describe_capabilities())
        return instance
