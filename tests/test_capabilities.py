from __future__ import annotations

import json
import copy
import unittest
from pathlib import Path

from mediacenter.capabilities import (
    MODEL_CAPABILITIES, WORKER_MODELS, WorkerCapabilityError, capability_for,
    validate_options, validate_worker_capability, validate_worker_request,
    worker_capability_for,
)
from mediacenter.residency import residency_modes_for
from mediacenter.domain import ServiceKind


class CapabilityTests(unittest.TestCase):
    def test_gpu_residency_modes_do_not_mislabel_offloaded_adapters(self) -> None:
        for model in ("qwen-image-2512", "minimax-h3-ref2va",
                      "ltx-2.3-distilled", "wan2.2-i2v-a14b",
                      "hunyuanvideo-1.5-720p-t2v", "wan2.1-t2v-1.3b"):
            with self.subTest(model=model):
                self.assertEqual(residency_modes_for(model), ("on_demand",))
        self.assertEqual(residency_modes_for("sdxl-base-1.0"),
                         ("on_demand", "idle", "resident"))
        self.assertEqual(residency_modes_for("krea-2-turbo"),
                         ("on_demand", "idle", "resident"))

    def test_wan21_rejects_silent_dimension_and_frame_adjustment(self) -> None:
        from mediacenter.capabilities import WorkerCapabilityError, validate_worker_request
        base = {"prompt":"test","width":832,"height":480,"num_frames":33}
        validate_worker_request("wan2.1-t2v-1.3b", "video.generate", base, [], [])
        for changed in ({"width":833}, {"height":481}, {"num_frames":34}):
            with self.subTest(changed=changed), self.assertRaises(WorkerCapabilityError):
                validate_worker_request("wan2.1-t2v-1.3b", "video.generate", {**base, **changed}, [], [])

    def test_complex_video_frame_congruence_is_enforced(self) -> None:
        from mediacenter.capabilities import WorkerCapabilityError,validate_worker_request,worker_capability_for
        cases={"minimax-h3-ref2va":(124,125),"ltx-2.3-distilled":(121,122),
               "wan2.2-i2v-a14b":(81,82),"hunyuanvideo-1.5-720p-t2v":(61,62)}
        for model,(valid,invalid) in cases.items():
            capability=worker_capability_for(model);reference=capability["reference_media"]
            inputs=[] if reference["minimum"]==0 else [{"asset_id":"asset","revision":"v1","media_type":"image/png"}]
            validate_worker_request(model,"video.generate",{"prompt":"test","num_frames":valid},inputs,[])
            with self.subTest(model=model),self.assertRaises(WorkerCapabilityError):
                validate_worker_request(model,"video.generate",{"prompt":"test","num_frames":invalid},inputs,[])
    def test_h3_quantization_matches_locked_torchao_api(self) -> None:
        driver = (Path(__file__).resolve().parents[1] / "mediacenter" / "adapters" /
                  "h3.py").read_text(encoding="utf-8")
        self.assertIn("Int8WeightOnlyConfig()", driver)
        self.assertNotIn("Int8WeightOnlyConfig(version=", driver)
        self.assertEqual(driver.count("low_cpu_mem_usage=True"), 3)
        self.assertNotIn("low_cpu_mem_usage=False", driver)
        self.assertNotIn("use_stream=True", driver)
        self.assertEqual(driver.count(".to(pipe._execution_device)"), 2)
        self.assertEqual(driver.count("onload_device=device"), 1)
        self.assertIn("pretrained_model_name_or_path=str(root)", driver)
        self.assertIn("local_files_only=True", driver)

    def test_h3_container_uses_diffusers_compatible_torchao(self) -> None:
        lock = (Path(__file__).resolve().parents[1] / "containers" / "video-models" /
                "requirements.txt").read_text(encoding="utf-8")
        self.assertIn("torchao==0.15.0", lock)

    def test_video_models_expose_model_specific_workflow_guidance(self) -> None:
        for model_key in ("minimax-h3-ref2va", "ltx-2.3-distilled",
                          "wan2.2-i2v-a14b", "hunyuanvideo-1.5-720p-t2v"):
            guide = capability_for(model_key, ServiceKind.VIDEO)["workflow_guide"]
            self.assertEqual(len(guide["steps"]), 3)
            self.assertTrue(guide["prompt_template"])
        self.assertIn("<Picture 1>", capability_for(
            "minimax-h3-ref2va", ServiceKind.VIDEO)["workflow_guide"]["prompt_template"])

    def test_real_driver_option_contracts_are_distinct(self) -> None:
        expected = {
            ("sdxl-base-1.0", ServiceKind.IMAGE): {"seed", "negative_prompt", "width", "height", "steps", "guidance_scale"},
            ("z-image-turbo", ServiceKind.IMAGE): {"seed", "width", "height", "steps", "guidance_scale"},
            ("qwen-image-2512", ServiceKind.IMAGE): {"seed", "negative_prompt", "width", "height", "steps", "true_cfg_scale"},
            ("krea-2-turbo", ServiceKind.IMAGE): {"seed", "width", "height", "steps", "guidance_scale"},
            ("illustrious-xl-v2.0", ServiceKind.IMAGE): {"seed", "negative_prompt", "width", "height", "steps", "guidance_scale"},
            ("wan2.1-t2v-1.3b", ServiceKind.VIDEO): {"seed", "negative_prompt", "width", "height", "num_frames", "steps", "guidance_scale", "fps"},
            ("cosyvoice2-0.5b", ServiceKind.SPEECH): {"speed"},
            ("musicgen-small", ServiceKind.MUSIC): {"seed", "duration_seconds", "guidance_scale", "temperature"},
        }
        for (model_key, kind), fields in expected.items():
            actual = {field["key"] for field in capability_for(model_key, kind)["options"]}
            self.assertEqual(actual, fields)

    def test_zimage_dimensions_and_unknown_options_fail_before_queue(self) -> None:
        with self.assertRaisesRegex(ValueError, "32 的倍数"):
            validate_options("z-image-turbo", ServiceKind.IMAGE, {"width": 1000})
        with self.assertRaisesRegex(ValueError, "不支持"):
            validate_options("cosyvoice2-0.5b", ServiceKind.SPEECH, {"voice": "placeholder"})

    def test_ltx_rejects_single_step_nan_schedule(self) -> None:
        with self.assertRaisesRegex(ValueError, "推理步数必须在 2 到 50 之间"):
            validate_options("ltx-2.3-distilled", ServiceKind.VIDEO, {"steps": 1})

    def test_large_video_models_require_dimensions_without_silent_crop(self) -> None:
        for model_key in ("wan2.2-i2v-a14b", "hunyuanvideo-1.5-720p-t2v"):
            validate_options(model_key, ServiceKind.VIDEO, {"width": 640, "height": 352})
            with self.assertRaisesRegex(ValueError, "16 的倍数"):
                validate_options(model_key, ServiceKind.VIDEO, {"width": 640, "height": 360})

    def test_new_image_models_expose_and_enforce_model_specific_sizes(self) -> None:
        qwen = capability_for("qwen-image-2512", ServiceKind.IMAGE)
        self.assertEqual(len(qwen["size_presets"]), 7)
        self.assertEqual(qwen["size_presets"][0], {"label": "1:1", "width": 1328, "height": 1328})
        validate_options("qwen-image-2512", ServiceKind.IMAGE,
                         {"width": 1664, "height": 928, "true_cfg_scale": 4.0})
        with self.assertRaisesRegex(ValueError, "尺寸预设"):
            validate_options("qwen-image-2512", ServiceKind.IMAGE,
                             {"width": 1024, "height": 1024})
        with self.assertRaisesRegex(ValueError, "64 的倍数"):
            validate_options("illustrious-xl-v2.0", ServiceKind.IMAGE, {"width": 1000})
        validate_options("krea-2-turbo", ServiceKind.IMAGE,
                         {"width": 1824, "height": 1024, "steps": 8,
                          "guidance_scale": 0.0})
        with self.assertRaisesRegex(ValueError, "16 的倍数"):
            validate_options("krea-2-turbo", ServiceKind.IMAGE, {"width": 1030})

    def test_realesrgan_models_are_single_input_upscale_workflows(self) -> None:
        keys = ("realesrgan-x2plus", "realesrgan-x4plus", "realesrgan-x4plus-anime-6b")
        for key in keys:
            capability = capability_for(key, ServiceKind.IMAGE)
            self.assertEqual(capability["workflow"], "upscale")
            self.assertEqual(capability["input_contract"], {
                "minimum": 1, "maximum": 1, "accept": ["image/"],
            })
            self.assertEqual(capability["options"], [])
            validate_options(key, ServiceKind.IMAGE, {})


class WorkerCapabilityTests(unittest.TestCase):
    def request(self, model="sdxl-base-1.0", operation="image.generate", parameters=None,
                inputs=None, loras=None, capability=None):
        return validate_worker_request(model, operation, {"prompt": "a lake"} if parameters is None else parameters,
                                       [] if inputs is None else inputs, [] if loras is None else loras,
                                       capability=capability)

    def test_all_existing_models_explicitly_declare_features_without_mutating_legacy(self):
        before = copy.deepcopy(MODEL_CAPABILITIES)
        self.assertEqual(set(WORKER_MODELS), set(MODEL_CAPABILITIES))
        for key in WORKER_MODELS:
            with self.subTest(model=key):
                capability = worker_capability_for(key)
                validate_worker_capability(capability)
                lora_models = {'sdxl-base-1.0', 'illustrious-xl-v2.0',
                               'sdxl-single-file'}
                self.assertEqual(capability["lora"]["supported"], key in lora_models)
                self.assertEqual(capability["lora"]["maximum"], 1 if key in lora_models else 0)
                self.assertEqual(capability["options"], MODEL_CAPABILITIES[key]["options"])
                reference = capability["reference_media"]
                self.assertEqual(reference["supported"], reference["maximum"] > 0)
                inputs = [{"asset_id": "input-1", "revision": "r1", "media_type": "image/png"}
                          for _ in range(reference["minimum"])]
                self.request(key, capability["operation"], inputs=inputs)
        detached = worker_capability_for("sdxl-base-1.0")
        detached["options"][0]["default"] = 100
        self.assertEqual(before, MODEL_CAPABILITIES)

    def test_unknown_model_is_rejected_across_control_and_worker_contracts(self):
        with self.assertRaisesRegex(ValueError, "unknown_model"):
            capability_for("not-real", ServiceKind.IMAGE)
        with self.assertRaisesRegex(ValueError, "unknown_model"):
            validate_options("not-real", ServiceKind.IMAGE, {})
        with self.assertRaisesRegex(WorkerCapabilityError, "unknown_model"):
            worker_capability_for("not-real")
        with self.assertRaisesRegex(WorkerCapabilityError, "unknown_model"):
            self.request(model="not-real")

    def test_unsupported_features_and_operations_rejected(self):
        with self.assertRaisesRegex(WorkerCapabilityError, "unsupported_operation"):
            self.request(operation="image.upscale")
        with self.assertRaisesRegex(WorkerCapabilityError, "unsupported_lora"):
            self.request(model='z-image-turbo', loras=[{"asset_id": "lora-1"}])
        with self.assertRaisesRegex(WorkerCapabilityError, "unsupported_inputs"):
            self.request(inputs=[{"asset_id": "input-1"}])

    def test_parameter_limits_types_unknowns_and_combinations(self):
        for options in ({"width": 511}, {"height": 1537}, {"steps": 0}, {"seed": True},
                        {"seed": 1.0}, {"guidance_scale": float("nan")}, {"seed": 2**10000},
                        {"negative_prompt": "x" * 4001}, {"width": [1024]}, {"url": "secret"}):
            with self.subTest(options=list(options)):
                with self.assertRaises(WorkerCapabilityError):
                    self.request(parameters={"prompt": "x", **options})
        with self.assertRaisesRegex(WorkerCapabilityError, "parameter_multiple"):
            self.request("z-image-turbo", parameters={"prompt": "x", "width": 1000})
        with self.assertRaisesRegex(WorkerCapabilityError, "parameter_combination"):
            self.request("qwen-image-2512", parameters={"prompt": "x", "width": 1024, "height": 1024})
        self.request(parameters={"prompt": "x", "seed": 0, "width": 512, "height": 1536, "steps": 80})

    def test_reference_media_contract_requires_versions_and_allowed_types(self):
        reference = {"asset_id": "frame-1", "revision": "r1", "media_type": "image/png"}
        self.request("wan2.2-i2v-a14b", "video.generate", inputs=[reference])
        for inputs in ([], [reference, reference], [{**reference, "revision": "../r1"}],
                       [{**reference, "media_type": "audio/wav"}], [{**reference, "path": "/tmp"}]):
            with self.assertRaises(WorkerCapabilityError):
                self.request("wan2.2-i2v-a14b", "video.generate", inputs=inputs)
        h3 = [{"asset_id": "image-1", "revision": "r1", "media_type": "image/png"},
              {"asset_id": "audio-1", "revision": "r2", "media_type": "audio/wav"},
              {"asset_id": "video-1", "revision": "r3", "media_type": "video/mp4"}]
        self.request("minimax-h3-ref2va", "video.generate", inputs=h3)

    def test_fake_lora_support_checks_family_revision_weight_count_and_duplicates(self):
        capability = worker_capability_for("sdxl-base-1.0")
        capability["model_key"] = "fixture-sdxl"
        capability["lora"] = {"supported": True, "maximum": 2, "minimum_weight": -1, "maximum_weight": 2}
        lora = {"asset_id": "lora-1", "revision": "r1", "family": "sdxl", "weight": 0.7}
        self.request("fixture-sdxl", loras=[lora], capability=capability)
        for loras in ([{**lora, "weight": 2.1}], [{**lora, "weight": True}], [{**lora, "weight": float("inf")}],
                      [{**lora, "family": "wan"}], [{**lora, "revision": "https://host"}],
                      [{key: value for key, value in lora.items() if key != "revision"}],
                      [lora, lora], [lora, lora, lora]):
            with self.subTest(loras=str(loras)[:80]):
                with self.assertRaises(WorkerCapabilityError):
                    self.request("fixture-sdxl", loras=loras, capability=capability)
        with self.assertRaisesRegex(WorkerCapabilityError, "model_mismatch"):
            self.request("sdxl-base-1.0", loras=[lora], capability=capability)
        self.assertTrue(worker_capability_for("sdxl-base-1.0")["lora"]["supported"])

    def test_capability_schema_rejects_unknown_or_malformed_declarations(self):
        base = worker_capability_for("sdxl-base-1.0")
        for field in base:
            capability = copy.deepcopy(base)
            del capability[field]
            with self.assertRaises(WorkerCapabilityError):
                validate_worker_capability(capability)
        for field, value in (("schema", "mc.capability/2"), ("operation", []), ("options", {}),
                             ("lora", {"supported": True}), ("reference_media", {"supported": False}),
                             ("constraints", [{"type": "execute_python"}])):
            capability = copy.deepcopy(base)
            capability[field] = value
            with self.assertRaises(WorkerCapabilityError):
                validate_worker_capability(capability)
        capability = copy.deepcopy(base)
        capability["secret_key"] = "never echo"
        with self.assertRaisesRegex(WorkerCapabilityError, "^invalid_capability$"):
            validate_worker_capability(capability)
        capability = copy.deepcopy(base)
        capability["reference_media"]["accept"] = ["image/"]
        with self.assertRaises(WorkerCapabilityError):
            validate_worker_capability(capability)

    def test_direct_worker_api_json_only_and_prompt_text_is_not_scanned(self):
        self.request(parameters={"prompt": 'A poster with API Key, /tmp and __import__("os").'})
        for parameters in ({"prompt": b"bytes"}, {"prompt": "\ud800"}, {"prompt": object()},
                           {"prompt": " "}, {"prompt": "x" * 16001}, {"prompt": "x", 1: "key"}):
            with self.assertRaises(WorkerCapabilityError):
                self.request(parameters=parameters)
        self.request("realesrgan-x2plus", "image.upscale", parameters={"prompt": ""},
                     inputs=[{"asset_id": "input-1", "revision": "r1", "media_type": "image/png"}])

    def test_capability_field_type_mutations_have_safe_errors(self):
        original = worker_capability_for("sdxl-base-1.0")
        for container in (None, "reference_media", "lora", "option"):
            fields = (original if container is None else original["options"][0] if container == "option"
                      else original[container])
            for key in fields:
                for value in (None, False, [], {}, 0, 0.5, "private-content"):
                    capability = copy.deepcopy(original)
                    target = (capability if container is None else capability["options"][0]
                              if container == "option" else capability[container])
                    target[key] = value
                    with self.subTest(container=container, key=key, value=value):
                        try:
                            validate_worker_capability(capability)
                        except WorkerCapabilityError as error:
                            self.assertEqual(str(error), error.code)
                            self.assertNotIn("private-content", str(error))

    def test_capability_constraints_require_numeric_dimensions_and_valid_defaults(self):
        invalid = []
        for constraint in ({"type": "pixel_area", "minimum": 1, "maximum": 4194304},
                           {"type": "allowed_sizes", "values": [[1024, 1024]]}):
            capability = worker_capability_for("sdxl-base-1.0")
            capability["constraints"] = [constraint]
            width = next(field for field in capability["options"] if field["key"] == "width")
            width.clear()
            width.update(key="width", type="string", default="1024", max_length=4)
            invalid.append(capability)
        capability = worker_capability_for("z-image-turbo")
        next(field for field in capability["options"] if field["key"] == "width")["default"] = 1025
        invalid.append(capability)
        capability = worker_capability_for("qwen-image-2512")
        next(field for field in capability["options"] if field["key"] == "width")["default"] = 1024
        invalid.append(capability)
        capability = worker_capability_for("sdxl-base-1.0")
        capability["constraints"] = [{"type": "pixel_area", "minimum": 1, "maximum": 1024}]
        invalid.append(capability)
        for index, capability in enumerate(invalid):
            with self.subTest(case=index):
                with self.assertRaisesRegex(WorkerCapabilityError, "^invalid_capability$"):
                    validate_worker_capability(capability)
                with self.assertRaisesRegex(WorkerCapabilityError, "^invalid_capability$"):
                    self.request(capability["model_key"], capability=capability)


if __name__ == "__main__":
    unittest.main()
