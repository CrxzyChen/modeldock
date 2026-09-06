"""Immutable OCI release contracts used by the control plane."""
from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urlsplit


class RuntimeContractError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class RuntimeRelease:
    """Validate one digest-pinned OCI image release declaration."""

    def __init__(self, value):
        from .config import ContainerError, ImageApproval

        try:
            expected = {"schema", "release_id", "image", "artifact", "sdk_digest", "adapter_id"}
            if type(value) is not dict or set(value) != expected:
                raise RuntimeContractError("runtime_release_fields_invalid")
            if type(value["schema"]) is not int or value["schema"] != 1:
                raise RuntimeContractError("runtime_release_schema_invalid")
            for field in ("release_id", "adapter_id"):
                if not isinstance(value[field], str) or not re.fullmatch(
                        r"[a-z0-9][a-z0-9._-]{0,127}", value[field]):
                    raise RuntimeContractError("runtime_release_identity_invalid")
            if not isinstance(value["sdk_digest"], str) or not re.fullmatch(
                    r"[0-9a-f]{64}", value["sdk_digest"]):
                raise RuntimeContractError("runtime_sdk_digest_invalid")

            image = value["image"]
            image_fields = {"reference", "image_id", "platform", "entrypoint", "command", "environment"}
            if type(image) is not dict or set(image) != image_fields:
                raise RuntimeContractError("runtime_image_contract_invalid")
            if any(type(image[key]) is not list for key in ("entrypoint", "command", "environment")):
                raise RuntimeContractError("runtime_image_contract_invalid")
            ImageApproval(**dict(image, **{
                key: tuple(image[key]) for key in ("entrypoint", "command", "environment")
            }))

            artifact = value["artifact"]
            if type(artifact) is not dict or set(artifact) != {"format", "url", "sha256", "byte_size"}:
                raise RuntimeContractError("runtime_artifact_contract_invalid")
            if artifact["format"] != "oci-layout-tar":
                raise RuntimeContractError("runtime_artifact_format_invalid")
            if (type(artifact["byte_size"]) is not int
                    or not 0 < artifact["byte_size"] <= 32 * 1024**3
                    or not isinstance(artifact["sha256"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"])):
                raise RuntimeContractError("runtime_artifact_size_or_digest_invalid")
            url = urlsplit(artifact["url"])
            if (url.scheme != "https" or not url.hostname or url.username or url.password
                    or url.fragment or url.query or url.port not in (None, 443)
                    or not url.path.startswith("/")
                    or any(ord(character) < 33 or ord(character) > 126 for character in artifact["url"])):
                raise RuntimeContractError("runtime_artifact_url_invalid")
            self._serialized = json.dumps(
                value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        except RuntimeContractError:
            raise
        except (KeyError, TypeError, ValueError, ContainerError, RecursionError):
            raise RuntimeContractError("runtime_release_invalid") from None

    @property
    def data(self):
        return json.loads(self._serialized)

    @property
    def digest(self):
        return hashlib.sha256(self._serialized.encode()).hexdigest()

    @property
    def image_digest(self):
        return self.data["image"]["reference"].split("@", 1)[-1]

    def require_approved(self, approved_digests):
        if self.digest not in approved_digests:
            raise RuntimeContractError("runtime_release_not_approved")
        return self
