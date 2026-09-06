from __future__ import annotations

import hashlib
import json
import struct
import tempfile
import unittest
from pathlib import Path

from mediacenter.model_assets import ModelAssetManager
from mediacenter.model_imports import ModelInspectionError, inspect_safetensors
from mediacenter.repository import Repository


def tensor_file(header: dict, data: bytes) -> bytes:
    encoded = json.dumps(header, separators=(",", ":")).encode("utf-8")
    return struct.pack("<Q", len(encoded)) + encoded + data


class ModelImportInspectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write(self, name: str, data: bytes) -> Path:
        target = self.root / name
        target.write_bytes(data)
        return target

    def lora_bytes(self) -> bytes:
        metadata = {
            "modelspec.architecture": "stable-diffusion-xl-v1-base/lora",
            "modelspec.resolution": "1024x1024",
            "ss_network_alpha": "32.0",
            "ss_new_sd_model_hash": "a" * 64,
            "ss_tag_frequency": "x" * (128 * 1024),
        }
        header = {
            "__metadata__": metadata,
            "lora_unet_down.lora_down.weight": {
                "dtype": "F16", "shape": [64, 2], "data_offsets": [0, 256]},
            "lora_unet_down.lora_up.weight": {
                "dtype": "F16", "shape": [2, 64], "data_offsets": [256, 512]},
        }
        return tensor_file(header, b"\0" * 512)

    def test_lora_identity_is_extracted_without_persisting_training_bulk(self) -> None:
        result = inspect_safetensors(self.write("adapter.safetensors", self.lora_bytes()))
        self.assertEqual((result["suggested_role"], result["architecture_family"]),
                         ("lora", "sdxl"))
        self.assertEqual(result["metadata"]["declared_base_identity"], "a" * 64)
        self.assertEqual((result["metadata"]["lora_rank"], result["metadata"]["lora_alpha"]),
                         (64, 32))
        self.assertNotIn("ss_tag_frequency", result["metadata"])
        self.assertLess(len(json.dumps(result["metadata"])), 4096)

    def test_header_duplicate_key_overlap_and_role_mismatch_fail_closed(self) -> None:
        raw = b'{"weight":{"dtype":"F32","shape":[1],"data_offsets":[0,4]},' \
              b'"weight":{"dtype":"F32","shape":[1],"data_offsets":[0,4]}}'
        duplicate = self.write("duplicate.safetensors", struct.pack("<Q", len(raw)) + raw + b"\0" * 4)
        with self.assertRaises(ModelInspectionError) as duplicate_error:
            inspect_safetensors(duplicate)
        self.assertEqual(duplicate_error.exception.code, "safetensors_duplicate_key")

        overlap = tensor_file({
            "a": {"dtype": "F32", "shape": [1], "data_offsets": [0, 4]},
            "b": {"dtype": "F32", "shape": [1], "data_offsets": [2, 6]},
        }, b"\0" * 6)
        with self.assertRaises(ModelInspectionError) as overlap_error:
            inspect_safetensors(self.write("overlap.safetensors", overlap))
        self.assertEqual(overlap_error.exception.code, "safetensors_tensor_overlap")

        with self.assertRaises(ModelInspectionError) as role_error:
            inspect_safetensors(self.write("lora.safetensors", self.lora_bytes()),
                                role_hint="checkpoint")
        self.assertEqual(role_error.exception.code, "model_role_mismatch")

    def test_publication_persists_server_inspection_and_preflight_reuses_content(self) -> None:
        repository = Repository(self.root / "state.db")
        manager = ModelAssetManager(repository, self.root / "model-store")
        data = self.lora_bytes()
        digest = hashlib.sha256(data).hexdigest()
        transfer = manager.create_upload({
            "display_name": "Fixture LoRA", "media_kind": "image", "role": "lora",
            "format": "safetensors", "revision": "fixture-v1",
            "license_declared": "test-only",
            "files": [{"relative_path": "adapter.safetensors", "byte_size": len(data),
                       "sha256": digest}],
        })
        manager.append_upload_chunk(transfer["id"], transfer["files"][0]["id"], 0, data)
        completed = manager.complete_upload(transfer["id"])
        asset = manager.get_asset(completed["asset_id"])
        self.assertEqual((asset["architecture_family"], asset["tensor_precision"]),
                         ("sdxl", "f16"))
        self.assertEqual(asset["metadata"]["declared_base_identity"], "a" * 64)
        reuse = manager.preflight_import({"files": [{
            "relative_path": "adapter.safetensors", "byte_size": len(data), "sha256": digest,
        }]})
        self.assertEqual((reuse["disposition"], reuse["asset"]["id"]),
                         ("reuse", asset["id"]))

    def test_unsafe_training_and_pickle_extensions_are_refused_before_storage(self) -> None:
        manager = ModelAssetManager(Repository(self.root / "unsafe.db"), self.root / "unsafe-store")
        for filename in ("optimizer.pt", "state.pkl", "weights.pth", "model.ckpt", "remote.py"):
            with self.subTest(filename=filename), self.assertRaisesRegex(Exception, "Pickle"):
                manager.create_upload({
                    "display_name": "unsafe", "media_kind": "image", "role": "checkpoint",
                    "format": "diffusers", "revision": "v1", "license_declared": "unknown",
                    "files": [{"relative_path": filename, "byte_size": 1}],
                })


if __name__ == "__main__":
    unittest.main()
