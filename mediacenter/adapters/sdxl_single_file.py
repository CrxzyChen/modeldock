"""Controlled single-file SDXL adapter for immutable user asset revisions."""
from __future__ import annotations

from pathlib import Path

from .illustrious import IllustriousAdapter
from .sdxl import asset_path, checked, require


class SDXLSingleFileAdapter(IllustriousAdapter):
    """Load one verified checkpoint and an optional verified VAE.

    Pipeline configuration is part of the immutable Runtime image.  Neither a
    task nor asset metadata can select code, a repository, loader kwargs or a
    configuration path.
    """

    model_key = "sdxl-single-file"

    def __init__(self, *, binding, asset_bindings, outputs,
                 lora_directory="/mc-lora",
                 config_root=None):
        super().__init__(binding=binding, asset_bindings=asset_bindings,
                         outputs=outputs, lora_directory=lora_directory)
        self.config_root = str(
            Path(__file__).resolve().parents[1] / "sdxl_config"
            if config_root is None else config_root
        )

    @staticmethod
    def _single_safetensors(root, code):
        files = [item for item in root.iterdir()
                 if item.is_file() and item.name.endswith(".safetensors")]
        require(len(files) == 1, code)
        return files[0]

    def load(self, binding):
        root = self._main_asset(binding)
        config = checked(self.config_root)
        require(config.is_dir(), "sdxl_config_missing")
        import torch
        from diffusers import (AutoencoderKL, DPMSolverMultistepScheduler,
                               StableDiffusionXLPipeline)
        from peft.tuners.tuners_utils import BaseTunerLayer
        require(torch.cuda.is_available(), "cuda_unavailable")
        checkpoint = self._single_safetensors(root, "sdxl_checkpoint_invalid")
        optional = self.assets.get("optional")
        require(type(optional) is dict and set(optional) <= {"vae"},
                "optional_asset_binding_invalid")
        vae = None
        if "vae" in optional:
            vae_root = asset_path(optional["vae"])
            vae_file = self._single_safetensors(vae_root, "sdxl_vae_invalid")
            vae_config = checked(config / "vae")
            require(vae_config.is_dir(), "sdxl_vae_config_missing")
            vae = AutoencoderKL.from_single_file(
                str(vae_file), config=str(vae_config), torch_dtype=torch.float16,
                use_safetensors=True, local_files_only=True)
        self.torch, self.scheduler_class, self.tuner_class = (
            torch, DPMSolverMultistepScheduler, BaseTunerLayer)
        arguments = {
            "config": str(config), "torch_dtype": torch.float16,
            "use_safetensors": True, "local_files_only": True,
        }
        if vae is not None:
            arguments["vae"] = vae
        self.pipe = StableDiffusionXLPipeline.from_single_file(
            str(checkpoint), **arguments).to("cuda")
        self.pipe.enable_vae_slicing()
        self.scheduler_config = dict(self.pipe.scheduler.config)
        self._capture_lora_parameter_state()
        torch.cuda.synchronize()
