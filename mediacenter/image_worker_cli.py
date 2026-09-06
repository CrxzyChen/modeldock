"""Fixed multi-model image Worker entrypoint; no task-selected imports or paths."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import threading
from pathlib import Path, PurePosixPath

from .adapter import AdapterFactory
from .adapters.illustrious import image_capability_for
from .adapters.sdxl import checked, regular, require
from .transport import Identity
from .worker_common import digest
from .worker_journal import CommandAuthority, WorkerJournal
from .worker_runtime import WorkerRuntime

SDK_VERSION = "mc.image-sdk/1"
BOOTSTRAP_FIELDS = {"schema", "server_id", "instance_id", "worker_epoch", "binding",
                    "recovery_complete", "clock_trusted", "journal", "outputs", "adapter_id",
                    "redis", "asset_bindings", "lora_authority"}
ADAPTERS = {
    "illustrious-xl-v2.0": ("illustrious", "mediacenter.adapters.illustrious", "IllustriousAdapter", {"sdxl"}, {"sdxl-base-1.0"}),
    "sdxl-single-file": ("sdxl-single-file", "mediacenter.adapters.sdxl_single_file", "SDXLSingleFileAdapter", {"sdxl"}, set()),
    "z-image-turbo": ("zimage", "mediacenter.adapters.zimage", "ZImageAdapter", set(), set()),
    "qwen-image-2512": ("qwen-image", "mediacenter.adapters.qwen_image", "QwenImageAdapter", set(), set()),
    "krea-2-turbo": ("krea2", "mediacenter.adapters.krea2", "Krea2Adapter", set(), set()),
    "realesrgan-x2plus": ("realesrgan", "mediacenter.adapters.realesrgan", "RealESRGANAdapter", set(), set()),
    "realesrgan-x4plus": ("realesrgan", "mediacenter.adapters.realesrgan", "RealESRGANAdapter", set(), set()),
    "realesrgan-x4plus-anime-6b": ("realesrgan", "mediacenter.adapters.realesrgan", "RealESRGANAdapter", set(), set()),
}


def _mapping(value, binding, code="bootstrap_assets_invalid"):
    require(type(value) is dict and set(value) == {"asset_id", "revision", "manifest_digest", "path"}, code)
    require(type(value["asset_id"]) is str and value["asset_id"].startswith("mdl_")
            and type(value["revision"]) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}", value["revision"])
            and type(value["manifest_digest"]) is str and re.fullmatch(r"[0-9a-f]{64}", value["manifest_digest"])
            and value["path"] == "/mc-models/assets/" + value["asset_id"], code)
    if binding is not None:
        require(value["asset_id"] == binding["model_asset_id"]
                and value["revision"] == binding["model_asset_revision"], code)


def read_bootstrap(path="/mc-bootstrap.json"):
    path = checked(path)
    with regular(path) as stream:
        info = os.fstat(stream.fileno())
        require(os.name != "posix" or info.st_uid == os.getuid() and not info.st_mode & 0o077,
                "bootstrap_not_private")
        raw = stream.read(65537)
    require(len(raw) <= 65536, "bootstrap_limit")
    def pairs(items):
        result = {}
        for key, item in items:
            require(key not in result, "bootstrap_duplicate_key"); result[key] = item
        return result
    value = json.loads(raw, object_pairs_hook=pairs)
    require(type(value) is dict and set(value) == BOOTSTRAP_FIELDS
            and type(value["schema"]) is int and value["schema"] == 1, "bootstrap_invalid")
    identity = Identity(value["server_id"], value["instance_id"], value["worker_epoch"])
    require(value["recovery_complete"] is True and value["clock_trusted"] is True,
            "bootstrap_not_admitted")
    binding = value["binding"]
    require(type(binding) is dict and set(binding) == {"model_key", "recipe_revision",
        "model_asset_id", "model_asset_revision", "image_digest", "gpu_uuids", "capability_digest"},
        "bootstrap_binding_invalid")
    model_key = binding.get("model_key")
    require(model_key in ADAPTERS and value["adapter_id"] == ADAPTERS[model_key][0]
            and value["journal"] == "/mc-journal/worker.db" and value["outputs"] == "/mc-outputs"
            and binding["capability_digest"] == digest(image_capability_for(model_key)),
            "bootstrap_binding_invalid")
    for key in ("recipe_revision", "model_asset_id", "model_asset_revision"):
        require(type(binding[key]) is str and ".." not in binding[key]
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}", binding[key]),
                "bootstrap_binding_invalid")
    require(type(binding["image_digest"]) is str and re.fullmatch(r"sha256:[0-9a-f]{64}", binding["image_digest"])
            and type(binding["gpu_uuids"]) is list and 1 <= len(binding["gpu_uuids"]) <= 8
            and all(type(gpu) is str and re.fullmatch(r"GPU-[0-9a-fA-F-]{36}", gpu) for gpu in binding["gpu_uuids"])
            and len(set(binding["gpu_uuids"])) == len(binding["gpu_uuids"]), "bootstrap_gpu_invalid")
    require(value["lora_authority"] == {"path": "/mc-lora", "families": sorted(ADAPTERS[model_key][3])},
            "bootstrap_lora_authority_invalid")
    assets = value["asset_bindings"]
    expected_fields = {"main", "dependencies", "optional"} if model_key == "sdxl-single-file" else {"main", "dependencies"}
    require(type(assets) is dict and set(assets) == expected_fields
            and type(assets["dependencies"]) is dict
            and set(assets["dependencies"]) == ADAPTERS[model_key][4], "bootstrap_assets_invalid")
    _mapping(assets["main"], binding)
    for dependency in assets["dependencies"].values(): _mapping(dependency, None)
    if model_key == "sdxl-single-file":
        require(type(assets["optional"]) is dict
                and set(assets["optional"]) <= {"vae"}, "bootstrap_assets_invalid")
        for optional in assets["optional"].values(): _mapping(optional, None)
    endpoint = value["redis"]
    require(type(endpoint) is dict and set(endpoint) == {"username", "secret_file", "unix_socket"}
            and endpoint["username"] == "mc_w_" + digest([identity.server_id, identity.instance_id, identity.worker_epoch])
            and endpoint["secret_file"] == "/mc-worker-secret" and type(endpoint["unix_socket"]) is str,
            "bootstrap_redis_invalid")
    socket = PurePosixPath(endpoint["unix_socket"])
    require(socket.parent == PurePosixPath("/mc-redis") and str(socket) == endpoint["unix_socket"]
            and socket.name not in (".", ".."), "bootstrap_redis_invalid")
    return value, identity, hashlib.sha256(raw).hexdigest()


def create_runtime(path="/mc-bootstrap.json"):
    value, identity, evidence = read_bootstrap(path)
    from .redis_transport import RedisEndpoint, RedisTransport
    endpoint = RedisEndpoint(**value["redis"]); endpoint.options()
    checked(Path(value["journal"]).parent); checked(value["outputs"])
    model_key = value["binding"]["model_key"]
    adapter_id, module, name, families, _dependencies = ADAPTERS[model_key]
    if families: checked("/mc-lora")
    if adapter_id == "realesrgan": checked("/mc-inputs")
    journal = WorkerJournal(value["journal"], identity.instance_id,
                            {model_key: image_capability_for(model_key)})
    transport = RedisTransport(identity, endpoint, role="worker", consumer="worker-" + identity.worker_epoch)
    options = dict(binding=value["binding"], asset_bindings=value["asset_bindings"],
                   outputs=value["outputs"], lora_directory="/mc-lora")
    if adapter_id == "realesrgan": options["inputs"] = "/mc-inputs"
    factory = AdapterFactory(module, name, options)
    try:
        runtime = WorkerRuntime(identity, value["binding"], journal, factory, transport,
            command_authority=CommandAuthority(identity, digest(value["binding"]), evidence))
        runtime.admit(evidence=evidence, recovery_complete=True, clock_trusted=True)
        return runtime, transport
    except BaseException:
        if "runtime" in locals(): runtime.close()
        transport.close(); raise


def main(argv=None):
    parser = argparse.ArgumentParser(description="Fixed MediaCenter image Worker")
    parser.parse_args(argv)
    stop = threading.Event()
    for number in (signal.SIGINT, signal.SIGTERM): signal.signal(number, lambda *_: stop.set())
    runtime, transport = create_runtime()
    try:
        runtime.start(); stop.wait()
    finally:
        runtime.close(); transport.close()


if __name__ == "__main__": main()
