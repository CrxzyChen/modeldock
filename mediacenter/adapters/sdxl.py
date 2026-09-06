"""Resident SDXL adapter; model imports occur only in the inference subprocess.

All paths are trusted bootstrap/Server-published asset descriptors. The task
supplies identities, never paths, Python imports, loader kwargs or Hub names.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from ..adapter import Adapter
from ..capabilities import worker_capability_for
from ..worker_common import canonical, digest


def require(condition, code):
    if not condition:
        raise ValueError(code)


def checked(value):
    path = Path(value).absolute()
    for part in (*reversed(path.parents), path):
        info = part.lstat()
        require(not stat.S_ISLNK(info.st_mode) and not getattr(info, 'st_file_attributes', 0) & 0x400,
                'asset_path_rejected')
    return path


@contextmanager
def regular(value):
    path = checked(value)
    parents = [(p, p.stat().st_dev, p.stat().st_ino) for p in path.parents]
    before = path.stat()
    if os.name == 'posix':
        directories = [os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)]
        try:
            for part in path.parts[1:-1]:
                directories.append(os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directories[-1]))
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directories[-1])
        finally:
            for directory in reversed(directories): os.close(directory)
    else:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(fd, 'rb') as source:
        after = os.fstat(source.fileno())
        require(stat.S_ISREG(after.st_mode) and (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino),
                'asset_file_changed')
        require(all((p.stat().st_dev, p.stat().st_ino) == (dev, ino) for p, dev, ino in parents),
                'asset_parent_changed')
        yield source


def read_json(path, maximum=1024 * 1024):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, 'duplicate_json_key')
            result[key] = value
        return result
    with regular(path) as source:
        data = source.read(maximum + 1)
    require(len(data) <= maximum, 'asset_metadata_limit')
    return json.loads(data, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid_json')))


def verify_files(root, files):
    root = checked(root)
    require(root.is_dir() and type(files) is list and 1 <= len(files) <= 256, 'asset_manifest_invalid')
    seen = set()
    for item in files:
        require(type(item) is dict and set(item) == {'relative_path', 'sha256', 'byte_size'}, 'asset_manifest_invalid')
        relative = PurePosixPath(item['relative_path'])
        require(not relative.is_absolute() and relative.parts and '..' not in relative.parts
                and '\\' not in item['relative_path'] and str(relative) == item['relative_path']
                and str(relative) not in seen, 'asset_path_rejected')
        seen.add(str(relative))
        require(type(item['byte_size']) is int and 0 < item['byte_size'] <= 64 * 1024**3
                and type(item['sha256']) is str and re.fullmatch('[0-9a-f]{64}', item['sha256']), 'asset_manifest_invalid')
        sha = hashlib.sha256(); size = 0
        with regular(root.joinpath(*relative.parts)) as source:
            while block := source.read(min(1024 * 1024, item['byte_size'] + 1 - size)):
                size += len(block)
                require(size <= item['byte_size'], 'asset_content_changed')
                sha.update(block)
        require(size == item['byte_size'] and sha.hexdigest() == item['sha256'], 'asset_content_changed')
    return root


def asset_path(mapping):
    require(type(mapping) is dict and set(mapping) == {'asset_id', 'revision', 'manifest_digest', 'path'}, 'asset_mapping_invalid')
    root = checked(mapping['path'])
    value = read_json(root / 'manifest.json')
    require(type(value) is dict and set(value) == {'asset_id', 'manifest_digest', 'files'}
            and value['asset_id'] == mapping['asset_id'] and value['manifest_digest'] == mapping['manifest_digest'], 'asset_mapping_changed')
    sha = hashlib.sha256()
    for item in sorted(value['files'], key=lambda item: item['relative_path']):
        sha.update(item['relative_path'].encode()); sha.update(b'\0')
        sha.update(item['sha256'].encode('ascii')); sha.update(b'\0')
        sha.update(str(item['byte_size']).encode('ascii'))
    require(sha.hexdigest() == mapping['manifest_digest'], 'asset_manifest_changed')
    return verify_files(root, value['files'])


def lora_descriptor(directory, reference, *, model_binding, models_root='/mc-models'):
    require(type(model_binding) is dict and all(type(model_binding.get(name)) is str
            for name in ('model_asset_id', 'model_asset_revision', 'recipe_revision')),
            'lora_base_binding_invalid')
    key = digest([reference['asset_id'], reference['revision'],
                  model_binding['model_asset_id'], model_binding['model_asset_revision'],
                  model_binding['recipe_revision']])
    root = checked(directory)
    active = read_json(root / 'active.json')
    require(type(active) is dict and active.get('schema') == 1 and type(active.get('permits')) is dict,
            'lora_authority_invalid')
    permit = active['permits'].get(key)
    require(type(permit) is str and re.fullmatch('[0-9a-f]{64}', permit), 'lora_not_approved')
    value = read_json(root / (permit + '.json'))
    require(digest(value) == permit and set(value) == {'schema', 'asset_id', 'revision', 'family', 'manifest_digest',
            'license_digest', 'base_asset_id', 'base_revision', 'runtime_profile_digest',
            'compatibility_detector_version', 'compatibility_evidence_digest',
            'path', 'files', 'weight_name'} and value['schema'] == 1,
            'lora_descriptor_invalid')
    require(all(value[k] == reference[k] for k in ('asset_id', 'revision', 'family')) and value['family'] == 'sdxl',
            'lora_binding_changed')
    require(value['base_asset_id'] == model_binding['model_asset_id']
            and value['base_revision'] == model_binding['model_asset_revision']
            and value['runtime_profile_digest'] == model_binding['recipe_revision'],
            'lora_base_binding_changed')
    expected = Path('/mc-models/assets') / value['asset_id']
    require(Path(value['path']) == expected and len(value['files']) == 1
            and value['weight_name'] == value['files'][0]['relative_path']
            and value['weight_name'].endswith('.safetensors'), 'lora_path_invalid')
    actual = Path(models_root) / 'assets' / value['asset_id']
    verify_files(actual, value['files'])
    return dict(value, path=str(actual))


class SDXLAdapter(Adapter):
    def __init__(self, *, binding, asset_bindings, outputs, lora_directory):
        self.binding = dict(binding)
        self.assets = asset_bindings
        self.outputs = outputs
        self.lora_directory = lora_directory
        self.pipe = self.torch = self.scheduler_class = self.tuner_class = None
        self.scheduler_config = None
        self.dirty = False

    def describe_capabilities(self):
        return worker_capability_for('sdxl-base-1.0')

    def load(self, binding):
        require(self.pipe is None, 'model_already_loaded')
        require(all(binding.get(k) == self.binding[k] for k in ('model_key', 'recipe_revision', 'model_asset_id', 'model_asset_revision')),
                'model_binding_changed')
        root = asset_path(self.assets['main'])
        require(self.assets['main']['asset_id'] == self.binding['model_asset_id']
                and self.assets['main']['revision'] == self.binding['model_asset_revision'], 'model_asset_changed')
        import torch
        from diffusers import StableDiffusionXLPipeline, DPMSolverMultistepScheduler
        from peft.tuners.tuners_utils import BaseTunerLayer
        require(torch.cuda.is_available(), 'cuda_unavailable')
        self.torch, self.scheduler_class, self.tuner_class = torch, DPMSolverMultistepScheduler, BaseTunerLayer
        self.pipe = StableDiffusionXLPipeline.from_pretrained(str(root), torch_dtype=torch.float16,
            use_safetensors=True, variant='fp16', local_files_only=True).to('cuda')
        self.scheduler_config = dict(self.pipe.scheduler.config)
        self.pipe.enable_vae_slicing()
        torch.cuda.synchronize()

    def execute(self, request, progress, cancellation):
        self.validate_request(request)
        require(self.pipe is not None and not self.dirty, 'model_not_clean')
        self.dirty = True
        def canceled():
            require(not cancellation.is_set(), 'task_canceled')
        canceled()
        payload = request['payload']
        defaults = {field['key']: field['default'] for field in self.describe_capabilities()['options']}
        parameters = {**defaults, **payload['parameters']}
        self.pipe.scheduler = self.scheduler_class.from_config(self.scheduler_config, use_karras_sigmas=True)
        for reference in payload['loras']:
            value = lora_descriptor(self.lora_directory, reference,
                                    model_binding=self.binding)
            self.pipe.load_lora_weights(value['path'], weight_name=value['weight_name'], adapter_name='mc_task',
                local_files_only=True, use_safetensors=True, hotswap=False)
            self.pipe.set_adapters(['mc_task'], adapter_weights=[reference['weight']])
        generator = self.torch.Generator(device='cuda').manual_seed(parameters['seed'])
        def callback(_pipe, step, _timestep, kwargs):
            canceled()
            progress({'phase': 'generating', 'completed': step + 1, 'total': parameters['steps'], 'unit': 'steps'})
            return kwargs
        canceled()
        result = self.pipe(prompt=parameters['prompt'], negative_prompt=parameters['negative_prompt'],
            width=parameters['width'], height=parameters['height'], num_inference_steps=parameters['steps'],
            guidance_scale=parameters['guidance_scale'], generator=generator, callback_on_step_end=callback)
        canceled()
        image = result.images[0]
        require(image.size == (parameters['width'], parameters['height']), 'image_size_mismatch')
        return self._publish(request, image)

    def _publish(self, request, image):
        root = checked(self.outputs)
        for component in ('tasks', request['task_id'], request['attempt_id']):
            require(type(component) is str and re.fullmatch('[A-Za-z0-9][A-Za-z0-9_-]{0,127}', component), 'output_identity_invalid')
            root = root / component
            root.mkdir(mode=0o700, exist_ok=True)
            checked(root)
        path = root / 'artifact.png'
        with path.open('xb') as stream:
            image.save(stream, format='PNG')
            stream.flush(); os.fsync(stream.fileno())
        with regular(path) as stream:
            sha = hashlib.file_digest(stream, 'sha256').hexdigest() if hasattr(hashlib, 'file_digest') else hashlib.sha256(stream.read()).hexdigest()
            size = os.fstat(stream.fileno()).st_size
        manifest = {'asset_id': 'art_' + digest([request['task_id'], request['attempt_id']])[:32], 'revision': sha, 'sha256': sha}
        descriptor = dict(manifest, schema=1, **{k: request[k] for k in ('task_id', 'attempt_id', 'instance_id', 'worker_epoch')},
            command_message_id=request['message_id'], command_digest=digest(request), byte_size=size, media_type='image/png')
        with (root / 'manifest.json').open('xb') as stream:
            stream.write(canonical(descriptor).encode()); stream.flush(); os.fsync(stream.fileno())
        if os.name == 'posix':
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try: os.fsync(fd)
            finally: os.close(fd)
        return manifest

    def reset_task_state(self):
        require(self.pipe is not None, 'model_not_loaded')
        self.pipe.unload_lora_weights()
        for component in (self.pipe.unet, self.pipe.text_encoder, self.pipe.text_encoder_2):
            require(not getattr(component, 'peft_config', None) and not getattr(component, '_hf_peft_config_loaded', False)
                    and not any(isinstance(layer, self.tuner_class) for layer in component.modules()), 'lora_reset_unconfirmed')
        self.pipe.scheduler = self.scheduler_class.from_config(self.scheduler_config, use_karras_sigmas=True)
        self.pipe._interrupt = False
        self.pipe._cross_attention_kwargs = None
        self.pipe._denoising_end = None
        self.pipe._clip_skip = None
        self.torch.cuda.synchronize()
        self.dirty = False

    def unload(self):
        if self.pipe is not None:
            self.reset_task_state()
            self.pipe = None
            self.scheduler_config = None
            import gc
            gc.collect()
            self.torch.cuda.synchronize()
            self.torch.cuda.empty_cache()
