#!/usr/bin/env python3
"""Strict, read-only verification for MediaCenter release artifacts."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import tarfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import BinaryIO


RELEASE_KEYS = {
    "schema", "product", "version", "release_id", "created_at",
    "source_snapshot", "toolchain", "artifacts", "security", "verification",
}
FILE_KEYS = {"path", "bytes", "sha256"}
SOURCE_KEYS = {"kind", "git_repository", "digest", "files"}
ARTIFACT_KEYS = {
    "id", "platform", "kind", "path", "bytes", "sha256", "signed",
    "payload_manifest_sha256",
}
SECURITY_KEYS = {
    "credentials_embedded", "model_weights_embedded", "database_embedded",
    "media_embedded", "windows_packaged_paths",
}
TOOLCHAIN_KEYS = {"python", "node", "electron", "electron_builder"}
VERIFICATION_KEYS = {"command", "manifest_sha256_file", "status"}
SERVER_KEYS = {"schema", "product", "version", "root", "files", "security"}
SERVER_FILE_KEYS = {"path", "bytes", "sha256", "mode"}
SERVER_SECURITY_KEYS = {
    "credentials_embedded", "model_weights_embedded", "database_embedded",
    "media_embedded",
}
VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
DENIED_PARTS = {"models", "inputs", "artifacts", "credentials", "secrets"}
DENIED_NAMES = {".env", "api-key", "mediacenter.db", "mediacenter.env", "runtime.json"}
DENIED_SUFFIXES = {
    ".ckpt", ".pth", ".pt", ".safetensors", ".onnx", ".sqlite", ".sqlite3",
    ".bin", ".gguf", ".ggml", ".npy", ".npz",
    ".mp3", ".wav", ".flac", ".mp4", ".mov", ".avi", ".webm",
}
TEXT_SUFFIXES = {
    ".py", ".js", ".ts", ".vue", ".json", ".toml", ".md", ".txt",
    ".service", ".conf", ".example", ".sh", ".yml", ".yaml", ".css", ".html",
}
SECRET_PATTERNS = (
    re.compile(rb"(?im)^\s*MEDIACENTER_API_KEY\s*=\s*\S+"),
    re.compile(rb"(?im)^\s*(?:HF_TOKEN|HUGGING_FACE_HUB_TOKEN)\s*=\s*\S+"),
    re.compile(rb"(?i)authorization\s*:\s*bearer\s+[A-Za-z0-9._~+/-]{12,}"),
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)
MAX_SERVER_FILE_BYTES = 32 * 1024 * 1024
MAX_SERVER_PAYLOAD_BYTES = 512 * 1024 * 1024


class ReleaseValidationError(ValueError):
    pass


def require(condition: bool, code: str) -> None:
    if not condition:
        raise ReleaseValidationError(code)


def canonical(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest_stream(stream: BinaryIO) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        size += len(chunk)
        digest.update(chunk)
    return size, digest.hexdigest()


def digest_file(path: Path) -> tuple[int, str]:
    with path.open("rb") as stream:
        return digest_stream(stream)


def safe_relative(value: object, *, code: str = "release_path_invalid") -> str:
    require(isinstance(value, str) and value != "" and "\\" not in value and "\x00" not in value, code)
    path = PurePosixPath(value)
    require(not path.is_absolute() and value == path.as_posix(), code)
    require(all(part not in {"", ".", ".."} for part in path.parts), code)
    return value


def safe_repository_file(root: Path, relative: str) -> Path:
    relative = safe_relative(relative)
    path = root / relative
    require(path.is_file() and not path.is_symlink(), "release_file_missing_or_unsafe")
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise ReleaseValidationError("release_path_outside_root") from exc
    return path


def validate_sha(value: object, code: str = "release_sha256_invalid") -> str:
    require(isinstance(value, str) and SHA256.fullmatch(value) is not None, code)
    return value


def validate_file_record(value: object, keys: set[str] = FILE_KEYS) -> dict:
    require(isinstance(value, dict) and set(value) == keys, "release_file_record_invalid")
    safe_relative(value["path"])
    require(isinstance(value["bytes"], int) and not isinstance(value["bytes"], bool)
            and value["bytes"] >= 0, "release_file_size_invalid")
    validate_sha(value["sha256"])
    return value


def scan_payload(relative: str, content: bytes) -> None:
    path = PurePosixPath(relative)
    lowered = {part.lower() for part in path.parts}
    require(not (lowered & DENIED_PARTS), "release_payload_private_root_forbidden")
    require(path.name.lower() not in DENIED_NAMES, "release_payload_private_file_forbidden")
    require(path.suffix.lower() not in DENIED_SUFFIXES, "release_payload_large_asset_forbidden")
    if path.suffix.lower() in TEXT_SUFFIXES or path.name.endswith(".env.example"):
        require(len(content) <= 16 * 1024 * 1024, "release_text_payload_too_large")
        for pattern in SECRET_PATTERNS:
            require(pattern.search(content) is None, "release_payload_secret_detected")


def validate_server_manifest(value: object) -> dict:
    require(isinstance(value, dict) and set(value) == SERVER_KEYS, "server_manifest_schema_invalid")
    require(value["schema"] == "mc.server-bundle/1" and value["product"] == "MediaCenter",
            "server_manifest_identity_invalid")
    require(isinstance(value["version"], str) and VERSION.fullmatch(value["version"]) is not None,
            "server_manifest_version_invalid")
    require(value["root"] == f"MediaCenter-server-{value['version']}", "server_manifest_root_invalid")
    require(isinstance(value["security"], dict)
            and set(value["security"]) == SERVER_SECURITY_KEYS
            and all(value["security"][key] is False for key in SERVER_SECURITY_KEYS),
            "server_manifest_security_invalid")
    files = value["files"]
    require(isinstance(files, list) and files, "server_manifest_files_invalid")
    paths: list[str] = []
    for item in files:
        validate_file_record(item, SERVER_FILE_KEYS)
        require(item["mode"] in {420, 493}, "server_manifest_mode_invalid")
        require(item["bytes"] <= MAX_SERVER_FILE_BYTES, "server_manifest_file_too_large")
        paths.append(item["path"])
    require(paths == sorted(paths) and len(paths) == len(set(paths)), "server_manifest_file_order_invalid")
    require(sum(item["bytes"] for item in files) <= MAX_SERVER_PAYLOAD_BYTES,
            "server_manifest_payload_too_large")
    return value


def inspect_server_bundle(path: Path) -> dict:
    require(path.is_file() and not path.is_symlink(), "server_bundle_missing_or_unsafe")
    try:
        archive = tarfile.open(path, "r:gz")
    except (OSError, tarfile.TarError) as exc:
        raise ReleaseValidationError("server_bundle_invalid") from exc
    with archive:
        members = archive.getmembers()
        require(members and all(member.isfile() for member in members), "server_bundle_members_invalid")
        names = [safe_relative(member.name, code="server_bundle_path_invalid") for member in members]
        require(len(names) == len(set(names)), "server_bundle_duplicate_path")
        manifest_names = [name for name in names if name.endswith("/release/server-manifest.json")]
        require(len(manifest_names) == 1, "server_bundle_manifest_missing")
        manifest_name = manifest_names[0]
        stream = archive.extractfile(manifest_name)
        require(stream is not None, "server_bundle_manifest_unreadable")
        manifest_bytes = stream.read()
        try:
            manifest = validate_server_manifest(json.loads(manifest_bytes.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReleaseValidationError("server_bundle_manifest_invalid_json") from exc
        root = manifest["root"]
        require(manifest_name == f"{root}/release/server-manifest.json", "server_bundle_manifest_location_invalid")
        declared = {f"{root}/{item['path']}": item for item in manifest["files"]}
        actual = set(names) - {manifest_name}
        require(actual == set(declared), "server_bundle_file_set_mismatch")
        for name in sorted(actual):
            item = declared[name]
            member = archive.getmember(name)
            require(member.mode == item["mode"], "server_bundle_mode_mismatch")
            stream = archive.extractfile(member)
            require(stream is not None, "server_bundle_file_unreadable")
            content = stream.read()
            require(len(content) == item["bytes"] and digest_bytes(content) == item["sha256"],
                    "server_bundle_file_digest_mismatch")
            scan_payload(item["path"], content)
        return {
            "manifest": manifest,
            "manifest_sha256": digest_bytes(manifest_bytes),
            "files": len(actual),
        }


def validate_release_manifest(value: object) -> dict:
    require(isinstance(value, dict) and set(value) == RELEASE_KEYS, "release_manifest_schema_invalid")
    require(value["schema"] == "mc.release/1" and value["product"] == "MediaCenter",
            "release_manifest_identity_invalid")
    version = value["version"]
    require(isinstance(version, str) and VERSION.fullmatch(version) is not None,
            "release_version_invalid")
    require(value["release_id"] == f"mediacenter-{version}", "release_id_invalid")
    try:
        datetime.fromisoformat(value["created_at"].replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ReleaseValidationError("release_created_at_invalid") from exc
    source = value["source_snapshot"]
    require(isinstance(source, dict) and set(source) == SOURCE_KEYS
            and source["kind"] == "filesystem_sha256"
            and isinstance(source["git_repository"], bool), "release_source_snapshot_invalid")
    files = source["files"]
    require(isinstance(files, list) and files, "release_source_files_invalid")
    paths: list[str] = []
    for item in files:
        validate_file_record(item)
        paths.append(item["path"])
    require(paths == sorted(paths) and len(paths) == len(set(paths)), "release_source_order_invalid")
    require(source["digest"] == digest_bytes(canonical(files)), "release_source_digest_invalid")
    require(isinstance(value["toolchain"], dict) and set(value["toolchain"]) == TOOLCHAIN_KEYS
            and all(isinstance(item, str) and item for item in value["toolchain"].values()),
            "release_toolchain_invalid")
    artifacts = value["artifacts"]
    require(isinstance(artifacts, list) and len(artifacts) == 2, "release_artifacts_invalid")
    ids: list[str] = []
    for artifact in artifacts:
        require(isinstance(artifact, dict) and set(artifact) == ARTIFACT_KEYS,
                "release_artifact_record_invalid")
        safe_relative(artifact["path"])
        require(isinstance(artifact["bytes"], int) and artifact["bytes"] > 0,
                "release_artifact_size_invalid")
        validate_sha(artifact["sha256"])
        require(isinstance(artifact["signed"], bool), "release_artifact_signed_invalid")
        if artifact["payload_manifest_sha256"] is not None:
            validate_sha(artifact["payload_manifest_sha256"])
        ids.append(artifact["id"])
    require(ids == ["windows-installer", "linux-server-bundle"], "release_artifact_order_invalid")
    require(artifacts[0]["platform"] == "windows-x64" and artifacts[0]["kind"] == "nsis"
            and artifacts[0]["payload_manifest_sha256"] is None, "release_windows_artifact_invalid")
    require(artifacts[1]["platform"] == "linux-x86_64" and artifacts[1]["kind"] == "tar.gz"
            and artifacts[1]["payload_manifest_sha256"] is not None, "release_linux_artifact_invalid")
    security = value["security"]
    require(isinstance(security, dict) and set(security) == SECURITY_KEYS,
            "release_security_invalid")
    for key in ("credentials_embedded", "model_weights_embedded", "database_embedded", "media_embedded"):
        require(security[key] is False, "release_security_claim_invalid")
    require(security["windows_packaged_paths"] == ["electron/**/*", "client/dist/**/*", "package.json"],
            "release_windows_package_scope_invalid")
    verification = value["verification"]
    require(isinstance(verification, dict) and set(verification) == VERIFICATION_KEYS
            and verification["status"] == "self_verifiable", "release_verification_invalid")
    require(verification["command"] == [
        "python", "scripts/verify_release_bundle.py", "--manifest",
        f"dist/MediaCenter-{version}-release.json",
    ], "release_verification_command_invalid")
    require(verification["manifest_sha256_file"] == f"dist/MediaCenter-{version}-release.json.sha256",
            "release_verification_sidecar_invalid")
    return value


def verify_release(root: Path, manifest_path: Path) -> dict:
    root = root.resolve(strict=True)
    manifest_path = manifest_path.resolve(strict=True)
    try:
        manifest_path.relative_to(root)
    except ValueError as exc:
        raise ReleaseValidationError("release_manifest_outside_root") from exc
    require(manifest_path.is_file() and not manifest_path.is_symlink(), "release_manifest_missing_or_unsafe")
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = validate_release_manifest(json.loads(manifest_bytes.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseValidationError("release_manifest_invalid_json") from exc
    for item in manifest["source_snapshot"]["files"]:
        path = safe_repository_file(root, item["path"])
        size, sha = digest_file(path)
        require((size, sha) == (item["bytes"], item["sha256"]), "release_source_file_changed")
    linux = None
    for artifact in manifest["artifacts"]:
        path = safe_repository_file(root, artifact["path"])
        size, sha = digest_file(path)
        require((size, sha) == (artifact["bytes"], artifact["sha256"]),
                "release_artifact_digest_mismatch")
        if artifact["id"] == "linux-server-bundle":
            linux = inspect_server_bundle(path)
            require(linux["manifest"]["version"] == manifest["version"],
                    "release_server_version_mismatch")
            require(linux["manifest_sha256"] == artifact["payload_manifest_sha256"],
                    "release_server_manifest_digest_mismatch")
    sidecar = safe_repository_file(root, manifest["verification"]["manifest_sha256_file"])
    sidecar_value = sidecar.read_text(encoding="ascii").strip()
    require(sidecar_value == digest_bytes(manifest_bytes), "release_manifest_sidecar_mismatch")
    return {
        "schema": "mc.release-verification/1",
        "release_id": manifest["release_id"],
        "manifest_sha256": digest_bytes(manifest_bytes),
        "source_files": len(manifest["source_snapshot"]["files"]),
        "artifacts": len(manifest["artifacts"]),
        "server_files": linux["files"] if linux else 0,
        "status": "passed",
        "mutations_performed": [],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = verify_release(args.root, args.manifest if args.manifest.is_absolute()
                                else args.root / args.manifest)
    except (OSError, ReleaseValidationError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
