#!/usr/bin/env bash
set -euo pipefail

DATA_ROOT="/srv/mediacenter"
ENV_FILE="$DATA_ROOT/config/mediacenter.env"
set -a
source "$ENV_FILE"
set +a

HEALTH_JSON="$(curl --fail --silent "http://$MEDIACENTER_HOST:$MEDIACENTER_PORT/healthz")"
SERVICES_JSON="$(curl --fail --silent -H "X-API-Key: $MEDIACENTER_API_KEY" "http://$MEDIACENTER_HOST:$MEDIACENTER_PORT/api/v1/services")"
HARDWARE_JSON="$(curl --fail --silent -H "X-API-Key: $MEDIACENTER_API_KEY" "http://$MEDIACENTER_HOST:$MEDIACENTER_PORT/api/v1/hardware")"
RESOURCES_JSON="$(curl --fail --silent -H "X-API-Key: $MEDIACENTER_API_KEY" "http://$MEDIACENTER_HOST:$MEDIACENTER_PORT/api/v1/resources/gpus")"
export HEALTH_JSON SERVICES_JSON HARDWARE_JSON RESOURCES_JSON

python3 - <<'PY'
import json
import os


def payload(name):
    return json.loads(os.environ[name])


pool_raw = os.environ.get("MEDIACENTER_GPU_POOL", "")
assert pool_raw.strip(), "MEDIACENTER_GPU_POOL must be explicitly configured"
pool = [int(value.strip()) for value in pool_raw.split(",")]
assert all(index >= 0 for index in pool), "configured GPU indices must be non-negative"
assert len(pool) == len(set(pool)), "configured GPU indices must be unique"
pool = sorted(pool)

health = payload("HEALTH_JSON")
assert health["mode"] == "real"
assert health["services_ready"] == health["services_total"] == 4

services = payload("SERVICES_JSON")["items"]
assert len(services) == health["services_total"]
assert all(item["available"] and item["provider"] == "mediacenter-kernel" for item in services)
assert all(item["models"] for item in services), "every service must expose at least one model"
assert all(
    any(model["healthy"] for model in item["models"])
    for item in services
), "every service must expose at least one healthy model"

hardware = payload("HARDWARE_JSON")
assert hardware["gpu"]["available"], hardware["gpu"].get("error")
assert sorted(hardware["mediacenter"]["configured_gpu_indices"]) == pool
observed = {item["index"]: item for item in hardware["gpu"]["items"]}
missing = sorted(set(pool) - set(observed))
assert not missing, f"configured GPUs are missing from physical inventory: {missing}"

resources = payload("RESOURCES_JSON")
assert sorted(resources["configured_gpu_indices"]) == pool
resource_gpus = {item["index"]: item for item in resources["gpus"]}
assert set(observed) <= set(resource_gpus), "resource API must include every observed GPU"
assert all(
    item["configured_for_mediacenter"] == (index in pool)
    for index, item in resource_gpus.items()
)

model_count = sum(1 for item in services for model in item["models"] if model["healthy"])
process_count = sum(len(item.get("processes", [])) for item in observed.values())
print(json.dumps({
    "mode": health["mode"],
    "services_ready": health["services_ready"],
    "models_healthy": model_count,
    "configured_gpu_indices": pool,
    "observed_gpu_indices": sorted(observed),
    "observed_compute_processes": process_count,
}, ensure_ascii=False))
PY

systemctl --user is-active mediacenter.service
