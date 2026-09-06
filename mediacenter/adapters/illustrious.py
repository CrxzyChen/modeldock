"""Resident Illustrious XL adapter and shared image publication helpers."""
from __future__ import annotations

import gc
import hashlib
import os
import re
from pathlib import Path

from ..adapter import Adapter
from ..capabilities import worker_capability_for
from ..worker_common import canonical, digest
from .sdxl import asset_path, checked, lora_descriptor, regular, require


def image_capability_for(model_key):
    return worker_capability_for(model_key)


class ImageAdapterBase(Adapter):
    model_key = ""

    def __init__(self, *, binding, asset_bindings, outputs, lora_directory="/mc-lora",
                 inputs="/mc-inputs"):
        self.binding = dict(binding)
        self.assets = asset_bindings
        self.outputs = outputs
        self.lora_directory = lora_directory
        self.inputs = inputs
        self.pipe = None
        self.torch = None
        self.dirty = False

    def describe_capabilities(self):
        return image_capability_for(self.model_key)

    def _main_asset(self, binding):
        require(self.pipe is None, "model_already_loaded")
        require(all(binding.get(key) == self.binding.get(key) for key in
                    ("model_key", "recipe_revision", "model_asset_id", "model_asset_revision")),
                "model_binding_changed")
        require(binding.get("model_key") == self.model_key, "model_binding_changed")
        main = self.assets.get("main") if type(self.assets) is dict else None
        require(type(main) is dict and main.get("asset_id") == binding.get("model_asset_id")
                and main.get("revision") == binding.get("model_asset_revision"),
                "model_asset_changed")
        return asset_path(main)

    @staticmethod
    def _canceled(cancellation):
        require(not cancellation.is_set(), "task_canceled")

    def _publish(self, request, image):
        root = checked(self.outputs)
        for component in ("tasks", request["task_id"], request["attempt_id"]):
            require(type(component) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", component),
                    "output_identity_invalid")
            root = root / component
            root.mkdir(mode=0o700, exist_ok=True)
            checked(root)
        path = root / "artifact.png"
        with path.open("xb") as stream:
            image.save(stream, format="PNG")
            stream.flush(); os.fsync(stream.fileno())
        with regular(path) as stream:
            sha = hashlib.sha256(stream.read()).hexdigest()
            size = os.fstat(stream.fileno()).st_size
        manifest = {"asset_id": "art_" + digest([request["task_id"], request["attempt_id"]])[:32],
                    "revision": sha, "sha256": sha}
        descriptor = dict(manifest, schema=1, **{key: request[key] for key in
                          ("task_id", "attempt_id", "instance_id", "worker_epoch")},
                          command_message_id=request["message_id"], command_digest=digest(request),
                          byte_size=size, media_type="image/png")
        with (root / "manifest.json").open("xb") as stream:
            stream.write(canonical(descriptor).encode()); stream.flush(); os.fsync(stream.fileno())
        return manifest

    def reset_task_state(self):
        require(self.pipe is not None, "model_not_loaded")
        self._reset_pipeline()
        self.torch.cuda.synchronize()
        self.dirty = False

    def _reset_pipeline(self):
        if hasattr(self.pipe, "_interrupt"):
            self.pipe._interrupt = False

    def unload(self):
        if self.pipe is not None:
            if self.dirty:
                self.reset_task_state()
            self.pipe = None
            gc.collect()
            self.torch.cuda.synchronize()
            self.torch.cuda.empty_cache()


class IllustriousAdapter(ImageAdapterBase):
    model_key = "illustrious-xl-v2.0"

    def load(self, binding):
        root = self._main_asset(binding)
        dependencies = self.assets.get("dependencies")
        dependency = dependencies.get("sdxl-base-1.0") if type(dependencies) is dict else None
        require(type(dependency) is dict, "model_dependency_missing")
        config = asset_path(dependency)
        import torch
        from diffusers import DPMSolverMultistepScheduler, StableDiffusionXLPipeline
        from peft.tuners.tuners_utils import BaseTunerLayer
        require(torch.cuda.is_available(), "cuda_unavailable")
        files = [item for item in root.iterdir() if item.name.endswith(".safetensors")]
        require(len(files) == 1, "illustrious_checkpoint_invalid")
        self.torch, self.scheduler_class, self.tuner_class = torch, DPMSolverMultistepScheduler, BaseTunerLayer
        self.pipe = StableDiffusionXLPipeline.from_single_file(
            str(files[0]), config=str(config), torch_dtype=torch.float16,
            use_safetensors=True, local_files_only=True).to("cuda")
        self.pipe.enable_vae_slicing()
        self.scheduler_config = dict(self.pipe.scheduler.config)
        self._capture_lora_parameter_state()
        torch.cuda.synchronize()

    def _capture_lora_parameter_state(self):
        # PEFT freezes the base parameters on injection. Unwrapping its layers
        # does not restore those flags; even a no_grad inference can then take
        # a different numerical path. Keep identities/flags, not tensor copies.
        self._lora_parameter_state = {
            name: {key: (id(value), value.requires_grad)
                   for key, value in getattr(self.pipe, name).named_parameters()}
            for name in ("unet", "text_encoder", "text_encoder_2")}

    def _restore_lora_parameter_state(self):
        baseline = getattr(self, "_lora_parameter_state", None)
        require(baseline is not None, "lora_reset_unconfirmed")
        for name, expected in baseline.items():
            parameters = dict(getattr(self.pipe, name).named_parameters())
            require(parameters.keys() == expected.keys(), "lora_reset_unconfirmed")
            for key, (identity, requires_grad) in expected.items():
                parameter = parameters[key]
                require(id(parameter) == identity, "lora_reset_unconfirmed")
                if parameter.requires_grad != requires_grad:
                    parameter.requires_grad_(requires_grad)
                require(parameter.requires_grad == requires_grad, "lora_reset_unconfirmed")

    def execute(self, request, progress, cancellation):
        self.validate_request(request)
        require(self.pipe is not None and not self.dirty, "model_not_clean")
        self.dirty = True; self._canceled(cancellation)
        payload = request["payload"]
        defaults = {field["key"]: field["default"] for field in self.describe_capabilities()["options"]}
        parameters = {**defaults, **payload["parameters"]}
        self.pipe.scheduler = self.scheduler_class.from_config(self.scheduler_config, use_karras_sigmas=True)
        for reference in payload["loras"]:
            value = lora_descriptor(self.lora_directory, reference,
                                    model_binding=self.binding)
            self.pipe.load_lora_weights(value["path"], weight_name=value["weight_name"],
                                        adapter_name="mc_task", local_files_only=True,
                                        use_safetensors=True, hotswap=False)
            self.pipe.set_adapters(["mc_task"], adapter_weights=[reference["weight"]])
        generator = self.torch.Generator(device="cuda").manual_seed(parameters["seed"])
        def callback(_pipe, step, _timestep, values):
            self._canceled(cancellation)
            progress({"phase": "generating", "completed": step + 1,
                      "total": parameters["steps"], "unit": "steps"})
            return values
        result = self.pipe(prompt=parameters["prompt"], negative_prompt=parameters["negative_prompt"],
            width=parameters["width"], height=parameters["height"],
            num_inference_steps=parameters["steps"], guidance_scale=parameters["guidance_scale"],
            generator=generator, callback_on_step_end=callback)
        self._canceled(cancellation)
        require(result.images[0].size == (parameters["width"], parameters["height"]), "image_size_mismatch")
        return self._publish(request, result.images[0])

    def _reset_pipeline(self):
        self.pipe.unload_lora_weights()
        for component in (self.pipe.unet, self.pipe.text_encoder, self.pipe.text_encoder_2):
            require(not getattr(component, "peft_config", None)
                    and not getattr(component, "_hf_peft_config_loaded", False)
                    and not any(isinstance(layer, self.tuner_class) for layer in component.modules()),
                    "lora_reset_unconfirmed")
        self._restore_lora_parameter_state()
        self.pipe.scheduler = self.scheduler_class.from_config(self.scheduler_config, use_karras_sigmas=True)
        super()._reset_pipeline()
