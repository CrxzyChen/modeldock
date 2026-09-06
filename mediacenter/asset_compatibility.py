from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

from .repository import Repository


DETECTOR_VERSION = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
VERDICTS = {"exact", "compatible", "experimental", "incompatible", "unknown"}
SUBJECT_ROLES = {"lora", "vae"}
CURRENT_DETECTOR_VERSION = "mc-sdxl-2"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def evidence_digest(value: dict[str, Any]) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class AssetCompatibilityError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


class AssetCompatibilityManager:
    """Creates immutable compatibility evidence; it never rewrites user labels."""

    def __init__(self, repository: Repository, detector_version: str = CURRENT_DETECTOR_VERSION):
        if not DETECTOR_VERSION.fullmatch(detector_version):
            raise ValueError("invalid detector version")
        self.repository = repository
        self.detector_version = detector_version

    def assess(self, subject_asset_id: str, base_asset_id: str, *,
               persist: bool = True) -> dict[str, Any]:
        subject = self.repository.get_model_asset(subject_asset_id)
        base = self.repository.get_model_asset(base_asset_id)
        if subject is None or base is None:
            raise AssetCompatibilityError("model_asset_not_found", "兼容判断引用的模型资产不存在", 404)
        if subject["state"] != "ready" or base["state"] != "ready":
            raise AssetCompatibilityError("model_asset_not_ready", "兼容判断只接受 ready 资产", 409)
        if subject["id"] == base["id"]:
            raise AssetCompatibilityError("same_asset_compatibility", "资产不能与自身建立兼容关系")

        evidence = self.evaluate(subject, base, self.detector_version)
        record = {
            "subject_asset_id": subject["id"], "subject_revision": subject["revision"],
            "base_asset_id": base["id"], "base_revision": base["revision"],
            "detector_version": self.detector_version, "verdict": evidence["verdict"],
            "reason_codes": evidence["reason_codes"], "evidence": evidence,
            "evidence_digest": evidence_digest(evidence), "created_at": utc_now(),
        }
        if not persist:
            return {**record, "disposition": "preview"}
        try:
            disposition, stored = self.repository.put_asset_compatibility(record)
        except ValueError as exc:
            if str(exc) == "asset_compatibility_conflict":
                raise AssetCompatibilityError(str(exc), "同一检测器版本已存在不同兼容证据", 409) from None
            raise
        return {**stored, "disposition": disposition}

    @classmethod
    def evaluate(cls, subject: dict[str, Any], base: dict[str, Any],
                 detector_version: str) -> dict[str, Any]:
        """Pure metadata check, shared by publication and transactional admission."""
        reasons: list[str] = []
        if base["media_kind"] != "image" or subject["media_kind"] != "image":
            verdict = "incompatible"
            reasons.append("media_kind_not_image")
        elif base["role"] != "checkpoint":
            verdict = "incompatible"
            reasons.append("base_role_not_checkpoint")
        elif subject["role"] not in SUBJECT_ROLES:
            verdict = "incompatible"
            reasons.append("subject_role_not_supported")
        else:
            subject_family = subject.get("architecture_family") or "unknown"
            base_family = base.get("architecture_family") or "unknown"
            declared = subject.get("metadata", {}).get("declared_base_identity")
            identities = {base["manifest_digest"], f"{base['id']}@{base['revision']}"}
            files = base.get("files", [])
            if base["format"] == "safetensors" and len(files) == 1:
                identities.add(files[0]["sha256"])
            if subject_family == "unknown" or base_family == "unknown":
                verdict = "unknown"
                reasons.append("architecture_family_unknown")
            elif subject_family != base_family:
                verdict = "incompatible"
                reasons.append("architecture_family_mismatch")
            elif declared in identities:
                verdict = "exact"
                reasons.append("declared_base_identity_exact")
            elif declared:
                verdict = "incompatible"
                reasons.append("declared_base_identity_mismatch")
            else:
                verdict = "compatible"
                reasons.append("architecture_and_role_compatible")

        return {
            "subject": cls._identity(subject),
            "base": cls._identity(base),
            "verdict": verdict,
            "reason_codes": reasons,
            "detector_version": detector_version,
        }

    def require_task_compatible(self, subject_asset_id: str, subject_revision: str,
                                base_asset_id: str, base_revision: str,
                                *, allow_experimental: bool = False) -> dict[str, Any]:
        with self.repository._connect() as db:
            return self.require_in_transaction(
                db, subject_asset_id, subject_revision, base_asset_id, base_revision,
                detector_version=self.detector_version, allow_experimental=allow_experimental)

    @classmethod
    def require_in_transaction(cls, db, subject_asset_id, subject_revision,
                               base_asset_id, base_revision, *,
                               detector_version=CURRENT_DETECTOR_VERSION,
                               allow_experimental=False):
        row = db.execute("""SELECT * FROM asset_compatibility
            WHERE subject_asset_id=? AND subject_revision=? AND base_asset_id=?
              AND base_revision=? AND detector_version=?""",
            (subject_asset_id, subject_revision, base_asset_id, base_revision,
             detector_version)).fetchone()
        record = Repository._asset_compatibility(row) if row else None
        if record is None:
            raise AssetCompatibilityError("compatibility_not_verified", "资产兼容性尚未验证", 409)
        identities = []
        for asset_id, revision in ((subject_asset_id, subject_revision), (base_asset_id, base_revision)):
            asset_row = db.execute("SELECT * FROM model_assets WHERE id=?", (asset_id,)).fetchone()
            if not asset_row or asset_row["state"] != "ready" or asset_row["revision"] != revision:
                raise AssetCompatibilityError("compatibility_asset_changed", "兼容证据引用的资产已不可用", 409)
            asset = Repository._model_asset(asset_row)
            asset["files"] = [dict(file) for file in db.execute(
                "SELECT relative_path,sha256,byte_size FROM model_asset_files WHERE asset_id=? ORDER BY relative_path",
                (asset_id,))]
            identities.append(asset)
        expected = cls.evaluate(*identities, detector_version)
        if (record["evidence"] != expected or record["evidence_digest"] != evidence_digest(expected)
                or record["verdict"] != expected["verdict"]
                or record["reason_codes"] != expected["reason_codes"]):
            raise AssetCompatibilityError("compatibility_evidence_changed", "兼容证据与当前资产不一致", 409)
        if record["verdict"] in {"exact", "compatible"}:
            return record
        if record["verdict"] == "experimental" and allow_experimental:
            return record
        raise AssetCompatibilityError("asset_incompatible", "资产兼容性不允许本次绑定", 409)

    @staticmethod
    def _identity(asset: dict[str, Any]) -> dict[str, Any]:
        return {
            "asset_id": asset["id"], "revision": asset["revision"],
            "manifest_digest": asset["manifest_digest"], "role": asset["role"],
            "format": asset["format"],
            "media_kind": asset["media_kind"],
            "architecture_family": asset.get("architecture_family", "unknown"),
            "metadata_digest": asset.get("metadata_digest"),
            "files": [{key: item[key] for key in ("relative_path", "sha256", "byte_size")}
                      for item in sorted(asset.get("files", []), key=lambda item: item["relative_path"])],
        }
