from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

try:
    from PIL import Image
except ModuleNotFoundError:  # Runtime-v1 verification intentionally has no image extras.
    Image = None

from mediacenter.adapters.illustrious import ImageAdapterBase, IllustriousAdapter, image_capability_for
from mediacenter.adapters.krea2 import Krea2Adapter
from mediacenter.adapters.qwen_image import QwenImageAdapter
from mediacenter.adapters.realesrgan import MODELS, RealESRGANAdapter
from mediacenter.adapters.sdxl_single_file import SDXLSingleFileAdapter
from mediacenter.adapters.zimage import ZImageAdapter
from mediacenter.image_worker_cli import ADAPTERS, read_bootstrap
from mediacenter.capabilities import validate_worker_request, worker_capability_for
from mediacenter.runtime_provisioning import InstallationRuntime
from mediacenter.instance_policy import InstancePolicy
from mediacenter.reconciler import Reconciler
from mediacenter.repository import Repository
from mediacenter.task_state import TaskStateError
from mediacenter.worker_common import digest
from tests.test_resident_policy import fixture_capacity, package_identity, policy_value, seed_deployment


def binding(model, residency=None):
    value = dict(model_key=model, recipe_revision='recipe-1', model_asset_id='mdl_main',
                 model_asset_revision='r1', image_digest='sha256:'+'a'*64,
                 gpu_uuids=['GPU-'+'1'*36], capability_digest='b'*64)
    if residency is not None:
        value['residency'] = residency
    return value


def request(model, operation='image.generate', parameters=None, inputs=None, loras=None):
    return dict(schema='mc.envelope/1', message_id='msg_1', type='task.command',
        server_id='server', instance_id='instance', worker_epoch='epoch', sequence=1,
        created_at='2026-01-01T00:00:00Z', expires_at='2026-01-01T00:01:00Z',
        task_id='task_1', attempt_id='attempt_1', extensions={},
        payload=dict(model_key=model, operation=operation,
            parameters={'prompt':'a lake', **(parameters or {})}, inputs=inputs or [], loras=loras or []))


class FakeCuda:
    def synchronize(self): pass
    def empty_cache(self): pass


class FakeTorch:
    cuda = FakeCuda()
    class Generator:
        def __init__(self, device=None): self.device=device
        def manual_seed(self, seed): self.seed=seed; return self


class ImageAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        self.outputs=self.root/'outputs'; self.outputs.mkdir()

    def tearDown(self): self.temp.cleanup()

    def configured(self, adapter):
        adapter.torch=FakeTorch(); adapter.pipe=MagicMock(); adapter.pipe._interrupt=False
        adapter.pipe.return_value=SimpleNamespace(images=[Image.new('RGB',(1024,1024),'red')])
        return adapter

    def test_formal_request_chain_accepts_only_illustrious_sdxl_lora(self):
        lora={'asset_id':'lora-1','revision':'r1','family':'sdxl','weight':0.8}
        validate_worker_request('illustrious-xl-v2.0','image.generate',
            {'prompt':'a lake'},[],[lora])
        self.assertEqual(worker_capability_for('illustrious-xl-v2.0'),
                         image_capability_for('illustrious-xl-v2.0'))

    def test_service_center_persists_illustrious_lora_by_deployment_id_and_rejects_others(self):
        from mediacenter.domain import ServiceKind
        from mediacenter.runtime_provisioning import LoraAuthority
        from mediacenter.service_center import ServiceCenter, ServiceCenterError
        from tests.test_runtime_provisioning import LoraAuthorityTests

        lora_fixture=LoraAuthorityTests(); lora_fixture.setUp()
        fixture=SimpleNamespace(root=lora_fixture.fixture.root, repository=lora_fixture.repo, manager=lora_fixture.manager)
        try:
            authority=lora_fixture.authority
            base=lora_fixture.base
            template_digest='a'*64
            installation=SimpleNamespace(get=MagicMock(),templates={},lora_authority=authority,
                                         template=lambda _instance: SimpleNamespace(digest='a'*64))

            class Registry:
                installation_runtime=installation
                def __init__(self): self.spec=None
                def refresh(self): pass  # Explicit static model-spec fixture.
                def get(self,kind,key):
                    return self.spec if kind is ServiceKind.IMAGE and key==self.spec.model_key else None
            registry=Registry()
            center=ServiceCenter(fixture.repository,registry,fixture.root/'artifacts',model_assets=fixture.manager)
            reference=lora_fixture.reference

            def select(catalog):
                deployment='deployment-'+catalog
                model_binding=dict(lora_fixture.binding, model_key=catalog, dependencies=[])
                seed_deployment(fixture.repository,deployment,model_binding)
                center.runtime=SimpleNamespace(authority=SimpleNamespace(
                    deployment_binding=lambda _db,_deployment:model_binding))
                registry.spec=SimpleNamespace(model_key=deployment,catalog_key=catalog,asset_id=base['id'],
                    dependency_bindings=(),expected_runtime_digest=None,manifest_digest='recipe-v1',revision='r1',
                    kind=ServiceKind.IMAGE,health=lambda:(True,'ready'))
                installation.get.return_value={'catalog_key':catalog,'asset_id':base['id'],
                    'asset_revision':base['revision'],'recipe_digest':template_digest}
                installation.templates={catalog:SimpleNamespace(digest=template_digest)}
                return deployment

            deployment=select('illustrious-xl-v2.0')
            task=center.create_task({'service':'image','model':deployment,'prompt':'a lake','loras':[reference]})
            installation.get.assert_called_once_with(deployment)
            stored=fixture.repository.get_task(task['id'])
            self.assertEqual(stored['loras'],[reference])
            expected_binding=dict(lora_fixture.binding,model_key='illustrious-xl-v2.0',dependencies=[])
            self.assertEqual(stored['execution_binding'],expected_binding)
            for expected in ({'expected_binding':dict(expected_binding,recipe_revision='b'*64)},
                             {'expected_configuration':{'deployment_id':deployment,'config_revision':2,'config_digest':'c'*64}}):
                with self.assertRaises(ServiceCenterError) as conflict:
                    center.create_task(dict(service='image',model=deployment,prompt='stale',**expected))
                self.assertEqual(conflict.exception.code,'deployment_configuration_changed')
            self.assertEqual(len(fixture.repository.list_tasks()),1)
            image=center.create_asset('source.png','image/png',b'\x89PNG\r\n\x1a\nfixture')
            with fixture.repository._connect() as db:
                before=(db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0],
                        db.execute('SELECT COUNT(*) FROM runtime_lora_permits').fetchone()[0])
            for catalog in ('z-image-turbo','qwen-image-2512','realesrgan-x2plus',
                            'realesrgan-x4plus','realesrgan-x4plus-anime-6b',
                            'krea-2-turbo'):
                with self.subTest(catalog=catalog):
                    deployment=select(catalog)
                    payload={'service':'image','model':deployment,'prompt':'a lake','loras':[reference]}
                    if catalog.startswith('realesrgan-'): payload['inputs']=[image['id']]
                    with self.assertRaises(ServiceCenterError) as raised:
                        center.create_task(payload)
                    self.assertEqual(raised.exception.code,'unsupported_lora')
                    with fixture.repository._connect() as db:
                        self.assertEqual((db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0],
                                          db.execute('SELECT COUNT(*) FROM runtime_lora_permits').fetchone()[0]),before)
        finally:
            lora_fixture.tearDown()

    def test_installer_consumes_worker_contract_and_old_launch_module_is_retired(self):
        project=Path(__file__).resolve().parents[1]
        catalog=json.loads((project/'deploy'/'model_catalog.json').read_text(encoding='utf-8'))
        entries={item['catalog_key']:item for item in catalog['models'] if item['catalog_key'] in ADAPTERS}
        runtime=InstallationRuntime.__new__(InstallationRuntime)
        for model, entry in entries.items():
            worker=entry['worker_contract']
            self.assertTrue(worker['module'].startswith('mediacenter.adapters.'))
            template=SimpleNamespace(data={'recipe_digest':digest(entry),'release_digest':'release'})
            release=SimpleNamespace(data={'adapter_id':worker['adapter_id']})
            runtime.templates={model:template}
            runtime.images=SimpleNamespace(release=lambda _digest, value=release:value)
            self.assertIs(runtime.resolve(entry)[1],release)
            release.data={'adapter_id':'foreign'}
            with self.assertRaisesRegex(ValueError,'runtime_worker_contract_changed'):
                runtime.resolve(entry)

    def test_formal_image_models_reject_new_and_persisted_legacy_launches(self):
        project=Path(__file__).resolve().parents[1]
        self.assertEqual(list((project/'mediacenter'/'drivers').glob('*.py')), [])
        for model in set(ADAPTERS)-{'sdxl-base-1.0'}:
            with self.subTest(model=model), tempfile.TemporaryDirectory() as directory:
                repository=Repository(Path(directory)/'state.db')
                instance='retired-image'
                model_binding={'model_key':model,'recipe_revision':'recipe-v1',
                    'model_asset_id':'mdl_retired','model_asset_revision':'revision-v1','dependencies':[]}
                seed_deployment(repository,instance,model_binding)
                with repository._connect() as db:
                    db.execute('DELETE FROM instance_installation_bindings WHERE instance_id=?',(instance,))
                authority=InstancePolicy(repository)
                legacy=policy_value(model_binding,('GPU-one',),backend='legacy')
                with self.assertRaisesRegex(TaskStateError,'legacy_model_runtime_retired'):
                    authority.configure(instance,legacy)
                container=policy_value(model_binding,('GPU-one',),backend='container')
                with self.assertRaisesRegex(TaskStateError,'runtime_installation_binding_required'):
                    authority.configure(instance,container)

    def test_all_seven_models_use_explicit_resident_adapters(self):
        self.assertEqual(IllustriousAdapter.model_key, 'illustrious-xl-v2.0')
        self.assertEqual(ZImageAdapter.model_key, 'z-image-turbo')
        self.assertEqual(QwenImageAdapter.model_key, 'qwen-image-2512')
        self.assertEqual(Krea2Adapter.model_key, 'krea-2-turbo')
        self.assertEqual(SDXLSingleFileAdapter.model_key, 'sdxl-single-file')
        self.assertEqual(set(MODELS), {'realesrgan-x2plus','realesrgan-x4plus','realesrgan-x4plus-anime-6b'})
        for model, cls in [('illustrious-xl-v2.0',IllustriousAdapter),('z-image-turbo',ZImageAdapter),
                           ('qwen-image-2512',QwenImageAdapter),('krea-2-turbo',Krea2Adapter)]:
            adapter=cls(binding=binding(model),asset_bindings={},outputs=str(self.outputs))
            self.assertEqual(adapter.describe_capabilities()['model_key'],model)
        for model in MODELS:
            adapter=RealESRGANAdapter(binding=binding(model),asset_bindings={},outputs=str(self.outputs))
            self.assertEqual(adapter.describe_capabilities()['operation'],'image.upscale')

    def test_catalog_release_and_smoke_matrix_are_one_consistent_contract(self):
        project=Path(__file__).resolve().parents[1]
        catalog=json.loads((project/'deploy'/'model_catalog.json').read_text(encoding='utf-8'))
        fixture=json.loads((project/'tests'/'fixtures'/'container-smoke'/'images.json').read_text(encoding='utf-8'))
        release=json.loads((project/'containers'/'image-models'/'release.json').read_text(encoding='utf-8'))
        models={case['model'] for case in fixture['cases']}
        self.assertEqual(models,set(ADAPTERS)-{
            'sdxl-base-1.0','krea-2-turbo','sdxl-single-file'})
        self.assertEqual(release['release'],fixture['release'])
        entries={item['catalog_key']:item for item in catalog['models'] if item['catalog_key'] in models}
        self.assertEqual(set(entries),models)
        for model in models:
            worker=entries[model]['worker_contract']; adapter=ADAPTERS[model]
            self.assertEqual((worker['adapter_id'],worker['module'],worker['class']),adapter[:3])
            self.assertEqual(set(worker['lora_families']),adapter[3])
            self.assertEqual(set(worker['dependencies']),adapter[4])
            self.assertIn(model,release['targets'][worker['target']]['models'])
        docker=(project/'containers'/'image-models'/'Dockerfile').read_text(encoding='utf-8')
        self.assertIn('FROM runtime_parent AS common',docker)
        self.assertNotIn('http://',docker); self.assertNotIn('https://',docker)

        krea_fixture=json.loads((project/'tests'/'fixtures'/'container-smoke'/'krea2.json').read_text(encoding='utf-8'))
        krea_release=json.loads((project/'containers'/'krea2'/'release.json').read_text(encoding='utf-8'))
        krea=next(item for item in catalog['models'] if item['catalog_key']=='krea-2-turbo')
        self.assertEqual(krea_fixture['release'],krea_release['release'])
        self.assertEqual(krea['worker_contract']['release'],krea_release['release'])
        self.assertEqual(krea['worker_contract']['adapter_id'],krea_release['adapter_id'])
        self.assertEqual(krea_fixture['cases'][0]['model'],krea_release['model'])
        krea_docker=(project/'containers'/'krea2'/'Dockerfile').read_text(encoding='utf-8')
        self.assertIn('FROM runtime_parent',krea_docker)
        self.assertIn('Krea2Pipeline',krea_docker)
        self.assertNotIn('http://',krea_docker); self.assertNotIn('https://',krea_docker)

    def test_fixed_bootstrap_selects_only_the_six_bound_adapters(self):
        for model, (adapter_id, _module, _name, families, dependencies) in ADAPTERS.items():
            with self.subTest(model=model):
                value=dict(schema=1,server_id='server',instance_id='instance',worker_epoch='epoch',
                    recovery_complete=True,clock_trusted=True,journal='/mc-journal/worker.db',
                    outputs='/mc-outputs',adapter_id=adapter_id,
                    redis=dict(username='mc_w_'+digest(['server','instance','epoch']),
                               secret_file='/mc-worker-secret',unix_socket='/mc-redis/redis.sock'),
                    lora_authority=dict(path='/mc-lora',families=sorted(families)),
                    binding=dict(model_key=model,recipe_revision='r1',model_asset_id='mdl_main',
                        model_asset_revision='r1',image_digest='sha256:'+'a'*64,
                        gpu_uuids=['GPU-'+'1'*36],capability_digest=digest(image_capability_for(model))),
                    asset_bindings=dict(main=dict(asset_id='mdl_main',revision='r1',
                        manifest_digest='b'*64,path='/mc-models/assets/mdl_main'),dependencies={
                        key:dict(asset_id='mdl_dependency',revision='r1',manifest_digest='c'*64,
                                 path='/mc-models/assets/mdl_dependency') for key in dependencies}))
                if model == 'sdxl-single-file':
                    value['asset_bindings']['optional'] = {}
                path=self.root/(model+'.json'); path.write_text(json.dumps(value),encoding='utf-8'); path.chmod(0o600)
                actual,_identity,_sha=read_bootstrap(path); self.assertEqual(actual['adapter_id'],adapter_id)
                actual['asset_bindings']['dependencies']['unexpected']={}; path.write_text(json.dumps(actual),encoding='utf-8')
                with self.assertRaisesRegex(ValueError,'bootstrap_assets_invalid'): read_bootstrap(path)

    @unittest.skipIf(Image is None, "Pillow unavailable in Runtime-v1 verification")
    def test_zimage_execute_reports_steps_publishes_and_requires_reset(self):
        adapter=self.configured(ZImageAdapter(binding=binding('z-image-turbo'),asset_bindings={},outputs=str(self.outputs)))
        progress=[]; value=request('z-image-turbo',parameters={'width':1024,'height':1024,'steps':2,'seed':7,'guidance_scale':0.0})
        result=adapter.execute(value,progress.append,threading.Event())
        self.assertTrue((self.outputs/'tasks'/'task_1'/'attempt_1'/'artifact.png').is_file())
        self.assertTrue(result['asset_id'].startswith('art_')); self.assertTrue(adapter.dirty)
        with self.assertRaisesRegex(ValueError,'model_not_clean'): adapter.execute(value,progress.append,threading.Event())
        adapter.reset_task_state(); self.assertFalse(adapter.dirty)

    @unittest.skipIf(Image is None, "Pillow unavailable in Runtime-v1 verification")
    def test_qwen_rejects_non_official_size_before_pipeline(self):
        adapter=self.configured(QwenImageAdapter(binding=binding('qwen-image-2512'),asset_bindings={},outputs=str(self.outputs)))
        with self.assertRaisesRegex(ValueError,'parameter_combination'):
            adapter.execute(request('qwen-image-2512',parameters={'width':1024,'height':1024}),lambda _x:None,threading.Event())
        adapter.pipe.assert_not_called()

    @unittest.skipIf(Image is None, "Pillow unavailable in Runtime-v1 verification")
    def test_krea2_execute_uses_fixed_turbo_defaults_and_publishes(self):
        adapter=self.configured(Krea2Adapter(
            binding=binding('krea-2-turbo'),asset_bindings={},outputs=str(self.outputs)))
        progress=[]
        result=adapter.execute(request('krea-2-turbo'),progress.append,threading.Event())
        arguments=adapter.pipe.call_args.kwargs
        self.assertEqual((arguments['num_inference_steps'],arguments['guidance_scale']),
                         (8,0.0))
        self.assertNotIn('mu',arguments)
        self.assertEqual(result['asset_id'][:4],'art_')
        arguments['callback_on_step_end'](adapter.pipe,0,None,{})
        self.assertEqual(progress[-1],{'phase':'generating','completed':1,'total':8,'unit':'steps'})

    def test_krea2_load_rejects_non_distilled_snapshot(self):
        adapter=Krea2Adapter(binding=binding('krea-2-turbo'),asset_bindings={},outputs=str(self.outputs))
        adapter._main_asset=MagicMock(return_value=self.root/'model')
        fake_torch=SimpleNamespace(
            bfloat16='bf16', cuda=SimpleNamespace(is_available=lambda:True,synchronize=lambda:None))
        pipeline=MagicMock(); pipeline.from_pretrained.return_value=SimpleNamespace(is_distilled=False)
        with patch.dict('sys.modules',{'torch':fake_torch,
                                       'diffusers':SimpleNamespace(Krea2Pipeline=pipeline)}):
            with self.assertRaisesRegex(ValueError,'krea2_turbo_identity_invalid'):
                adapter.load(binding('krea-2-turbo','on_demand'))

    def test_krea2_load_uses_offload_only_for_on_demand(self):
        adapter=Krea2Adapter(binding=binding('krea-2-turbo'),asset_bindings={},outputs=str(self.outputs))
        adapter._main_asset=MagicMock(return_value=self.root/'model')
        fake_torch=SimpleNamespace(
            bfloat16='bf16', cuda=SimpleNamespace(is_available=lambda:True,synchronize=lambda:None))
        pipe=MagicMock(); loaded=MagicMock(); loaded.is_distilled=True
        pipe.from_pretrained.return_value=loaded
        with patch.dict('sys.modules',{'torch':fake_torch,
                                       'diffusers':SimpleNamespace(Krea2Pipeline=pipe)}):
            adapter.load(binding('krea-2-turbo','on_demand'))
        loaded.vae.enable_tiling.assert_called_once_with()
        loaded.enable_model_cpu_offload.assert_called_once_with(gpu_id=0)
        loaded.to.assert_not_called()

    def test_krea2_idle_and_resident_require_full_gpu_loading(self):
        for residency in ('idle','resident'):
            with self.subTest(residency=residency):
                adapter=Krea2Adapter(binding=binding('krea-2-turbo'),asset_bindings={},outputs=str(self.outputs))
                adapter._main_asset=MagicMock(return_value=self.root/'model')
                fake_torch=SimpleNamespace(
                    bfloat16='bf16', cuda=SimpleNamespace(is_available=lambda:True,synchronize=lambda:None))
                pipe=MagicMock(); loaded=MagicMock(); loaded.is_distilled=True
                pipe.from_pretrained.return_value=loaded
                with patch.dict('sys.modules',{'torch':fake_torch,
                                               'diffusers':SimpleNamespace(Krea2Pipeline=pipe)}):
                    adapter.load(binding('krea-2-turbo',residency))
                loaded.vae.enable_tiling.assert_called_once_with()
                loaded.to.assert_called_once_with('cuda')
                loaded.enable_model_cpu_offload.assert_not_called()

    @unittest.skipIf(Image is None, "Pillow unavailable in Runtime-v1 verification")
    def test_illustrious_is_only_additional_lora_model_and_reset_is_verified(self):
        adapter=self.configured(IllustriousAdapter(binding=binding('illustrious-xl-v2.0'),asset_bindings={},outputs=str(self.outputs)))
        adapter.scheduler_class=MagicMock(); adapter.scheduler_config={}; adapter.tuner_class=type('Tuner',(),{})
        component=MagicMock(); component.peft_config={}; component._hf_peft_config_loaded=False; component.modules.return_value=[]
        adapter.pipe.unet=component; adapter.pipe.text_encoder=component; adapter.pipe.text_encoder_2=component
        adapter._capture_lora_parameter_state()
        adapter.pipe.return_value=SimpleNamespace(images=[Image.new('RGB',(1024,1024),'blue')])
        lora={'asset_id':'lora-1','revision':'r1','family':'sdxl','weight':0.8}
        with patch('mediacenter.adapters.illustrious.lora_descriptor',return_value={'path':'/approved','weight_name':'a.safetensors'}):
            adapter.execute(request('illustrious-xl-v2.0',parameters={'width':1024,'height':1024},loras=[lora]),lambda _x:None,threading.Event())
        adapter.pipe.load_lora_weights.assert_called_once(); adapter.reset_task_state()
        adapter.pipe.unload_lora_weights.assert_called_once(); self.assertFalse(adapter.dirty)
        self.assertTrue(adapter.describe_capabilities()['lora']['supported'])

    def test_lora_reset_restores_original_grad_flags_and_rejects_parameter_replacement(self):
        class Parameter:
            def __init__(self, requires_grad):
                self.requires_grad = requires_grad
            def requires_grad_(self, value):
                self.requires_grad = value
                return self
        for cls in (IllustriousAdapter, SDXLSingleFileAdapter):
            for outcome in ('success', 'canceled', 'inference-error', 'partial-lora-load'):
                with self.subTest(adapter=cls.__name__, outcome=outcome):
                    adapter = cls(binding=binding(cls.model_key), asset_bindings={}, outputs=str(self.outputs))
                    adapter.pipe = MagicMock()
                    adapter.torch = FakeTorch()
                    adapter.scheduler_class = MagicMock()
                    adapter.scheduler_config = {}
                    adapter.tuner_class = type('Tuner', (), {})
                    components = []
                    for name in ('unet', 'text_encoder', 'text_encoder_2'):
                        component = MagicMock()
                        component.peft_config = {}
                        component._hf_peft_config_loaded = False
                        component.modules.return_value = []
                        component.named_parameters.return_value = [('weight', Parameter(True)), ('frozen', Parameter(False))]
                        setattr(adapter.pipe, name, component)
                        components.append(component)
                    adapter._capture_lora_parameter_state()
                    cancellation = threading.Event()
                    def inject(*_args, **_kwargs):
                        for component in components:
                            for _, parameter in component.named_parameters():
                                parameter.requires_grad_(False)
                        if outcome == 'partial-lora-load':
                            raise RuntimeError('partial injection')
                    def infer(**_kwargs):
                        if outcome == 'inference-error':
                            raise RuntimeError('inference failed')
                        if outcome == 'canceled':
                            cancellation.set()
                        return SimpleNamespace(images=[SimpleNamespace(size=(1024, 1024))])
                    adapter.pipe.load_lora_weights.side_effect = inject
                    adapter.pipe.side_effect = infer
                    adapter._publish = MagicMock(return_value={'diagnostic': 'unit-fixture'})
                    value = request(cls.model_key, loras=[{'asset_id':'lora-1','revision':'r1','family':'sdxl','weight':0.8}])
                    with patch('mediacenter.adapters.illustrious.lora_descriptor', return_value={'path':'/approved','weight_name':'a.safetensors'}):
                        if outcome == 'success':
                            adapter.execute(value, lambda _: None, cancellation)
                            adapter._publish.assert_called_once()
                        else:
                            with self.assertRaises((RuntimeError, ValueError)):
                                adapter.execute(value, lambda _: None, cancellation)
                            adapter._publish.assert_not_called()
                    self.assertTrue(adapter.dirty)
                    # Exercise reset after the real execute success/error path.
                    adapter.reset_task_state()
                    self.assertFalse(adapter.dirty)
                    for component in components:
                        self.assertEqual([p.requires_grad for _, p in component.named_parameters()], [True, False])
                    adapter.dirty = True
                    components[0].named_parameters.return_value[0] = ('weight', Parameter(True))
                    with self.assertRaisesRegex(ValueError, 'lora_reset_unconfirmed'):
                        adapter.reset_task_state()
                    self.assertTrue(adapter.dirty)

    @unittest.skipIf(Image is None, "Pillow unavailable in Runtime-v1 verification")
    def test_realesrgan_input_manifest_is_identity_and_hash_bound(self):
        inputs=self.root/'inputs'; inputs.mkdir()
        path=inputs/'input-1.png'; Image.new('RGB',(8,6),'green').save(path)
        data=path.read_bytes(); sha=hashlib.sha256(data).hexdigest()
        adapter=RealESRGANAdapter(binding=binding('realesrgan-x2plus'),asset_bindings={},
                                  outputs=str(self.outputs),inputs=str(inputs))
        reference={'asset_id':'input-1','revision':sha,'media_type':'image/png'}
        self.assertEqual(adapter._input_image(reference),path)
        path.write_bytes(data+b'x')
        with self.assertRaisesRegex(ValueError,'input_content_changed'): adapter._input_image(reference)

    @unittest.skipIf(Image is None, "Pillow unavailable in Runtime-v1 verification")
    def test_realesrgan_tile_loop_observes_cancel_and_reports_progress(self):
        inputs=self.root/'inputs'; inputs.mkdir()
        path=inputs/'input-1.png'; Image.new('RGB',(8,6),'green').save(path)
        data=path.read_bytes()
        adapter=RealESRGANAdapter(binding=binding('realesrgan-x2plus'),asset_bindings={},
            outputs=str(self.outputs),inputs=str(inputs))
        adapter.pipe=object(); adapter.torch=FakeTorch(); adapter.scale=2
        cancel=threading.Event(); events=[]
        def fake_enhance(_model,image,scale,tile,*,checkpoint,progress):
            progress(1,4); cancel.set(); checkpoint()
            return image.resize((image.width*scale,image.height*scale))
        value=request('realesrgan-x2plus',operation='image.upscale',
            inputs=[{'asset_id':'input-1','revision':hashlib.sha256(data).hexdigest(),'media_type':'image/png'}])
        with patch('mediacenter.adapters.realesrgan_core.auto_tile',return_value=64), \
             patch('mediacenter.adapters.realesrgan_core.enhance',side_effect=fake_enhance):
            with self.assertRaisesRegex(ValueError,'task_canceled'):
                adapter.execute(value,events.append,cancel)
        self.assertTrue(any(event['completed']==1 and event['total']==4 for event in events))


if __name__ == '__main__': unittest.main()
