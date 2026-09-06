#!/usr/bin/env python3
"""Attach one built SDXL single-file OCI and Runtime Profile to runtime.json.

This is an offline release-preparation step. It never imports an image, starts
a container, reads model weights or mutates the active runtime configuration.
Both outputs must be new files so an operator can review and atomically promote
the complete candidate later.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path

from mediacenter.container_releases import RuntimeRelease
from mediacenter.runtime_provisioning import RuntimeTemplate
from mediacenter.runtime_profiles import RuntimeProfileManager


SHA = re.compile(r"(?:sha256:)?[0-9a-f]{64}")
IDENTITY = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha_file(path: Path) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def _new_file(path: Path) -> Path:
    value = path.resolve()
    if value.exists() or not value.parent.is_dir():
        raise ValueError("output_must_be_new")
    return value


def _regular_file(path: Path) -> Path:
    value = path.resolve(strict=True)
    if not value.is_file() or value.is_symlink():
        raise ValueError("regular_file_required")
    return value


def _inside_any(path: Path, roots) -> bool:
    for raw in roots:
        root = Path(raw).resolve(strict=True)
        if not root.is_dir() or root.is_symlink():
            raise ValueError("local_artifact_root_invalid")
        if root == path.parent or root in path.parents:
            return True
    return False


def prepare(runtime: dict, evidence: dict, artifact: Path, *,
            release_id: str, artifact_url: str, profile_revision: int) -> tuple[dict, dict]:
    if (not IDENTITY.fullmatch(release_id)
            or not isinstance(profile_revision, int) or isinstance(profile_revision, bool)
            or profile_revision <= 0):
        raise ValueError("runtime_identity_invalid")
    required = {
        "schema", "status", "mode", "python_prefix", "manifest_digest",
        "config_digest", "archive_sha256", "archive_bytes", "sdk_digest",
        "entrypoint", "command", "environment",
    }
    if (not isinstance(evidence, dict) or not required <= set(evidence)
            or evidence["schema"] != "mc.derived-oci-build/1"
            or evidence["status"] != "passed" or evidence["mode"] != "sdk-only"
            or evidence["python_prefix"] != "/opt/python"
            or evidence["entrypoint"] != [
                "/opt/python/bin/python", "-B", "-u", "-m",
                "mediacenter.image_worker_cli"]
            or evidence["command"] != []
            or not SHA.fullmatch(evidence["manifest_digest"])
            or not SHA.fullmatch(evidence["config_digest"])
            or not re.fullmatch(r"[0-9a-f]{64}", evidence["sdk_digest"])):
        raise ValueError("derived_build_evidence_invalid")
    artifact = _regular_file(artifact)
    if sha_file(artifact) != (evidence["archive_sha256"], evidence["archive_bytes"]):
        raise ValueError("derived_artifact_identity_changed")
    if set(runtime) != {"server_id", "gpu_uuids", "engine", "installation"}:
        raise ValueError("runtime_configuration_invalid")
    installation = runtime["installation"]
    required_installation = {
        "releases", "approved_release_digests", "templates", "image_store",
        "download_hosts", "publisher", "package_root",
    }
    optional_installation = {
        "lora", "local_artifact_roots", "local_artifacts", "runtime_profiles",
    }
    if (not isinstance(installation, dict)
            or not required_installation <= set(installation)
            or not set(installation) <= required_installation | optional_installation):
        raise ValueError("installation_configuration_invalid")
    roots = installation.get("local_artifact_roots", [])
    if not isinstance(roots, list) or not _inside_any(artifact, roots):
        raise ValueError("derived_artifact_outside_approved_roots")

    manifest = evidence["manifest_digest"]
    if not manifest.startswith("sha256:"):
        manifest = "sha256:" + manifest
    config = evidence["config_digest"]
    if not config.startswith("sha256:"):
        config = "sha256:" + config
    release_data = {
        "schema": 1,
        "release_id": release_id,
        "adapter_id": "sdxl-single-file",
        "sdk_digest": evidence["sdk_digest"],
        "image": {
            "reference": "mediacenter.local/sdxl-single-file@" + manifest,
            "image_id": config,
            "platform": "linux/amd64",
            "entrypoint": list(evidence["entrypoint"]),
            "command": [],
            "environment": list(evidence["environment"]),
        },
        "artifact": {
            "format": "oci-layout-tar",
            "url": artifact_url,
            "sha256": evidence["archive_sha256"],
            "byte_size": evidence["archive_bytes"],
        },
    }
    release = RuntimeRelease(release_data)
    profile = RuntimeProfileManager.sdxl_single_file(release.image_digest)
    profile["revision"] = profile_revision

    candidate = json.loads(canonical(runtime))
    target = candidate["installation"]
    # Schema 10 derives permits from immutable compatibility evidence. Retire
    # static template approvals in the new candidate, retaining the source.
    if 'lora' in target:
        lora = target['lora']
        if (type(lora) is not dict or not set(lora) <= {'root', 'approvals'}
                or type(lora.get('root')) is not str):
            raise ValueError('lora_configuration_invalid')
        target['lora'] = {'root': lora['root']}
    releases = target["releases"]
    if (not isinstance(releases, list)
            or any(row.get("release_id") == release_id for row in releases)
            or any(row.get("image", {}).get("reference") == release_data["image"]["reference"]
                   for row in releases)):
        raise ValueError("runtime_release_conflict")
    approvals = target["approved_release_digests"]
    if not isinstance(approvals, list) or release.digest in approvals:
        raise ValueError("runtime_release_conflict")
    local = target.setdefault("local_artifacts", {})
    if not isinstance(local, dict) or release.digest in local:
        raise ValueError("runtime_release_conflict")
    profiles = target.setdefault("runtime_profiles", [])
    if (not isinstance(profiles, list)
            or any(row.get("profile_id") == profile["profile_id"]
                   and row.get("revision") == profile_revision for row in profiles)):
        raise ValueError("runtime_profile_conflict")
    releases.append(release_data)
    approvals.append(release.digest)
    approvals.sort()
    local[release.digest] = str(artifact)
    profiles.append(profile)
    profiles.sort(key=lambda row: (row["profile_id"], row["revision"]))
    return candidate, release_data


def exclusive(path: Path, raw: bytes) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def prepare_builtin(runtime: dict, evidence: dict, artifact: Path, *,
                    model_key: str, release_id: str, artifact_url: str) -> tuple[dict, dict]:
    """Advance an existing SDXL recipe's SDK without changing its dependencies.

    The old declarations remain available for rollback. Activating the changed
    template still requires the normal stopped-instance adoption workflow.
    """
    entrypoints = {'sdxl-base-1.0': ('sdxl', 'mediacenter.worker_cli'),
                   'illustrious-xl-v2.0': ('illustrious', 'mediacenter.image_worker_cli')}
    if model_key not in entrypoints or not IDENTITY.fullmatch(release_id):
        raise ValueError('builtin_sdk_identity_invalid')
    adapter, module = entrypoints[model_key]
    installation = runtime['installation']
    template = installation['templates'][model_key]
    parents = [RuntimeRelease(row) for row in installation['releases']
               if RuntimeRelease(row).digest == template['release_digest']]
    if len(parents) != 1:
        raise ValueError('builtin_sdk_parent_missing')
    parent = parents[0]
    expected_entrypoint = ['/opt/python/bin/python', '-B', '-u', '-m', module]
    if (evidence.get('schema') != 'mc.derived-oci-build/1'
            or evidence.get('status') != 'passed' or evidence.get('mode') != 'sdk-only'
            or evidence.get('python_prefix') != '/opt/python'
            or evidence.get('parent_config_digest') != parent.data['image']['image_id']
            or evidence.get('parent_manifest_digest') != parent.image_digest
            or evidence.get('entrypoint') != expected_entrypoint
            or evidence.get('command') != [] or parent.data['adapter_id'] != adapter):
        raise ValueError('builtin_sdk_parent_or_entrypoint_changed')
    artifact = _regular_file(artifact)
    if not _inside_any(artifact, installation['local_artifact_roots']):
        raise ValueError('derived_artifact_outside_approved_roots')
    if sha_file(artifact) != (evidence.get('archive_sha256'), evidence.get('archive_bytes')):
        raise ValueError('derived_artifact_identity_changed')
    data = parent.data
    data.update(release_id=release_id, sdk_digest=evidence['sdk_digest'])
    data['image'].update(reference=f'mediacenter.local/{model_key}@{evidence["manifest_digest"]}',
                         image_id=evidence['config_digest'], entrypoint=expected_entrypoint,
                         environment=evidence['environment'])
    data['artifact'].update(url=artifact_url, sha256=evidence['archive_sha256'],
                            byte_size=evidence['archive_bytes'])
    release = RuntimeRelease(data)
    if any(row['release_id'] == release_id or row['image']['reference'] == data['image']['reference']
           for row in installation['releases']):
        raise ValueError('runtime_release_conflict')
    candidate = json.loads(canonical(runtime))
    target = candidate['installation']
    target['releases'].append(data)
    target['approved_release_digests'] = sorted(set(target['approved_release_digests']) | {release.digest})
    target['local_artifacts'][release.digest] = str(artifact)
    target['templates'][model_key]['release_digest'] = release.digest
    RuntimeTemplate(target['templates'][model_key])
    return candidate, data


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", required=True)
    parser.add_argument("--runtime-config", required=True, type=Path)
    parser.add_argument("--build-evidence", required=True, type=Path)
    parser.add_argument("--artifact", required=True, type=Path)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--artifact-url", required=True)
    parser.add_argument("--profile-revision", required=True, type=int)
    parser.add_argument("--release-output", required=True, type=Path)
    parser.add_argument("--runtime-output", required=True, type=Path)
    parser.add_argument("--builtin-build", action='append', type=Path, default=[],
                        help='Operator-owned JSON with model_key, build_evidence, artifact, release_id, artifact_url.')
    args = parser.parse_args()
    release_output = _new_file(args.release_output)
    runtime_output = _new_file(args.runtime_output)
    runtime = json.loads(_regular_file(args.runtime_config).read_text(encoding="utf-8"))
    evidence = json.loads(_regular_file(args.build_evidence).read_text(encoding="utf-8"))
    candidate, release = prepare(
        runtime, evidence, args.artifact, release_id=args.release_id,
        artifact_url=args.artifact_url, profile_revision=args.profile_revision)
    builtin_releases = []
    for path in args.builtin_build:
        record = json.loads(_regular_file(path).read_text(encoding='utf-8'))
        if set(record) != {'model_key', 'build_evidence', 'artifact', 'release_id', 'artifact_url'}:
            raise ValueError('builtin_build_fields_invalid')
        build = json.loads(_regular_file(Path(record['build_evidence'])).read_text(encoding='utf-8'))
        candidate, derived = prepare_builtin(candidate, build, Path(record['artifact']),
            model_key=record['model_key'], release_id=record['release_id'], artifact_url=record['artifact_url'])
        builtin_releases.append(derived)
    exclusive(release_output, canonical(release) + b"\n")
    exclusive(runtime_output, canonical(candidate) + b"\n")
    print(canonical({
        "status": "prepared", "release": str(release_output),
        "runtime": str(runtime_output),
        "release_digest": RuntimeRelease(release).digest,
        "image_digest": release["image"]["reference"].rsplit("@", 1)[1],
        "profile_revision": args.profile_revision,
        "builtin_release_digests": [RuntimeRelease(item).digest for item in builtin_releases],
    }).decode("utf-8"))


if __name__ == "__main__":
    main()
