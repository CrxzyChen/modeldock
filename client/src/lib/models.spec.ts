import { beforeEach, describe, expect, it, vi } from "vitest";
import { mount, flushPromises } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { useAppStore } from '@/stores/app';
import ImageWorkspace from '@/views/ImageWorkspace.vue';
import ModelImportWizard from '@/components/ModelImportWizard.vue';
import ModelCenterView from '@/views/ModelCenterView.vue';
import { imageDraftKey, sameModelIdentity, loraCompatibility, mergeDeploymentOperations } from './models';
import { buildCatalogModels, canUninstall, isInstalled, isRunning, modelState, runtimeErrorHint, runtimeErrorLabel } from "./models";

describe('user model removal projection', () => {
  it('hides detached installations but keeps a removing model with recoverable status', () => {
    const deployment = { id:'wai-local',catalog_key:'sdxl-single-file',current_config_revision:1,install_state:'ready',
      service_state:'stopped', configuration_state:'applied', actual_state:'unloaded', removal_operation_id:'dop-remove' };
    let [item] = buildCatalogModels([], [deployment], [], []);
    expect(isInstalled(item)).toBe(true);
    expect(modelState(item)).toEqual({label:'正在卸载',tone:'busy'});
    expect(canUninstall(deployment)).toBe(false);
    [item] = buildCatalogModels([], [{...deployment,service_state:'stopping',removal_operation_state:'failed'}], [], []);
    expect(modelState(item)).toEqual({label:'卸载未完成',tone:'error'});
    expect(canUninstall(item.deployments[0])).toBe(true);
    expect(buildCatalogModels([], [{...deployment,install_state:'configured',removal_operation_id:null}], [], [])).toEqual([]);
  });
});

describe('existing model configuration editor', () => {
  function fixture() {
    setActivePinia(createPinia());
    window.mediaCenterDesktop = { request: vi.fn(async () => ({ ok: true, status: 200, data: { items: [] } })),
      listModelUploadSessions: vi.fn(async () => []) } as any;
    const store = useAppStore();
    store.connections = { activeProfileId: 'server-a', revision: 1, profiles: [
      { id: 'server-a', name: 'Server A', baseUrl: 'http://server-a', hasCredential: true },
      { id: 'server-b', name: 'Server B', baseUrl: 'http://server-b', hasCredential: true },
    ] } as any;
    const dep = { id: 'pony-local', catalog_key: 'sdxl-single-file', model_id: 'user/pony', label: 'Pony',
      install_state: 'ready', service_state: 'stopped', configuration_state: 'applied', actual_state: 'unloaded',
      current_config_revision: 1, pending_config_revision: null, kind: 'image', required_vram_mib: 16384,
      asset_id: 'pony', configuration_binding: { deployment_id: 'pony-local', config_revision: 1,
        config_digest: 'a'.repeat(64), runtime_profile_id: 'sdxl-single-file', runtime_profile_revision: 3,
        base_asset_id: 'pony', base_asset_revision: 'v6', vae_asset_id: null },
      instance_settings: { policy_version: 5, gpu_uuids: ['GPU-0'], sharing_mode: 'shared', external_reserve_mib: 8192,
        residency: 'on_demand', residency_modes: ['on_demand', 'idle', 'resident'], idle_minutes: 15, restart_recovery: false } };
    store.deployments = [dep];
    store.runtimeProfiles = [{ profile_id: 'sdxl-single-file', revision: 3, label: 'SDXL', image_digest: 'sha256:' + 'b'.repeat(64),
      profile_digest: 'c'.repeat(64), architecture_families: ['sdxl'], main_formats: ['safetensors'],
      optional_deployment_roles: ['vae'], task_roles: ['lora'], residency_modes: ['on_demand', 'idle', 'resident'], required_vram_mib: 16384 }];
    store.assets = [
      { id: 'pony', display_name: 'Pony', media_kind: 'image', role: 'checkpoint', state: 'ready', architecture_family: 'sdxl', format: 'safetensors' },
      { id: 'wai', display_name: 'WAI', media_kind: 'image', role: 'checkpoint', state: 'ready', architecture_family: 'sdxl', format: 'safetensors' },
      { id: 'vae', display_name: 'SDXL VAE', media_kind: 'image', role: 'vae', state: 'ready', architecture_family: 'sdxl', format: 'safetensors' },
    ];
    store.gpuResources = { gpus: [{ index: 0, uuid: 'GPU-0', name: 'GPU', configured_for_mediacenter: true, memory_free_mib: 44000 }] };
    vi.spyOn(store, 'refreshSnapshot').mockResolvedValue(true);
    const wrapper = mount(ModelCenterView, { global: { stubs: { Teleport: true } } });
    const vm = wrapper.vm as any;
    vm.openSettings(vm.models[0]);
    const plan = { operation: { deployment_id: dep.id, expected_configuration: { config_revision: 1, config_digest: 'a'.repeat(64), policy_version: 5 }, plan_digest: 'p' },
      runtime_profile: store.runtimeProfiles[0], compatibility: null, capacity: { schedulable: true, reason: '' },
      effects: { updates_existing: true, requires_restart: true, changed_fields: ['vae_asset_id'], uploads_bytes: 0, deletes_assets: false, preserves_desired_state: true, rebuilds_runtime_image: false } } as any;
    const edit = async () => { await flushPromises(); vm.settings.vae_asset_id = 'vae'; vm.settings.license_accepted = true; vm.markDirty(); await flushPromises(); };
    return { store, dep, wrapper, vm, plan, edit };
  }

  it('preserves drafts and focus on same-instance snapshots; version changes are explicit', async () => {
    const { store, dep, wrapper, vm, edit } = fixture(); await edit();
    const select = wrapper.find('.configuration-model-fields select').element;
    store.deployments = [{ ...structuredClone(dep), actual_state: 'loaded' }];
    await flushPromises();
    expect(vm.settings.vae_asset_id).toBe('vae'); expect(vm.settingsDirty).toBe(true);
    expect(wrapper.find('.configuration-model-fields select').element).toBe(select);
    expect(vm.settingsStale).toBe(false);
    store.deployments = [{ ...structuredClone(dep), instance_settings: { ...dep.instance_settings, policy_version: 6 } }];
    await flushPromises();
    expect(vm.settingsStale).toBe(true); expect(vm.settings.vae_asset_id).toBe('vae');
    expect(wrapper.text()).toContain('当前编辑内容未被覆盖');
    vm.resetSettings(); await flushPromises();
    expect(vm.settings.vae_asset_id).toBe(''); expect(vm.settingsSnapshot.instance_settings.policy_version).toBe(6);
    wrapper.unmount();
  });

  it('previews the captured complete contract and keeps one idempotency key for ambiguous POST retries', async () => {
    const { store, wrapper, vm, plan, edit } = fixture(); await edit();
    const preview = vi.spyOn(store, 'planUserDeployment').mockResolvedValue(plan);
    await vm.previewSettings();
    expect(preview.mock.calls[0][0]).toMatchObject({ deployment_id: 'pony-local', vae_asset_id: 'vae',
      expected_configuration: { config_revision: 1, config_digest: 'a'.repeat(64), policy_version: 5 },
      policy_options: { external_reserve_mib: 8192, idle_seconds: 900, restart_recovery: false } });
    expect(wrapper.text()).toContain('需安全重启容器');
    const post = vi.spyOn(store, 'createUserDeployment').mockResolvedValue(null);
    await vm.saveSettings(); await vm.saveSettings();
    expect(post).toHaveBeenCalledTimes(2); expect(post.mock.calls[0][1]).toBe(post.mock.calls[1][1]);
    expect(post.mock.calls[0][0]).toEqual(plan.operation);
    wrapper.unmount();
  });

  it('fences duplicate editing immediately on accepted and follows authoritative SSE results', async () => {
    const { store, wrapper, vm, plan, edit } = fixture(); await edit();
    vi.spyOn(store, 'planUserDeployment').mockResolvedValue(plan); await vm.previewSettings();
    const accepted = { id: 'dop-config', deployment_id: 'pony-local', state: 'accepted', payload: plan.operation } as any;
    const post = vi.spyOn(store, 'createUserDeployment').mockResolvedValue(accepted);
    await vm.saveSettings(); await flushPromises(); await vm.saveSettings();
    expect(post).toHaveBeenCalledTimes(1); expect(wrapper.get('.settings-controls').attributes('disabled')).toBeDefined();
    expect(vm.currentSettingsOperation.state).toBe('accepted');
    store.deploymentOperations = [{ ...accepted, state: 'ready' }]; await flushPromises();
    expect(vm.currentSettingsOperation.state).toBe('ready'); expect(wrapper.text()).toContain('配置已生效');
    wrapper.unmount();
  });

  it.each(['pending_revision', 'active_operation'])('blocks %s before policy enters applying', async kind => {
    const { store, wrapper, vm, edit } = fixture(); await edit();
    if (kind === 'pending_revision') store.deployments[0].pending_config_revision = 2;
    else store.deploymentOperations = [{ id: 'dop-config', deployment_id: 'pony-local', state: 'accepted' }] as any;
    await flushPromises();
    const preview = vi.spyOn(store, 'planUserDeployment'); await vm.previewSettings();
    expect(preview).not.toHaveBeenCalled(); expect(vm.settingsApplying).toBe(true);
    wrapper.unmount();
  });

  it.each(['server', 'close', 'instance'])('ignores a late preview after %s changes', async kind => {
    const { store, wrapper, vm, plan, edit, dep } = fixture(); await edit();
    let finish!: (value: any) => void;
    vi.spyOn(store, 'planUserDeployment').mockImplementation(() => new Promise(resolve => finish = resolve));
    const pending = vm.previewSettings();
    if (kind === 'server') store.connectionEpoch++;
    else if (kind === 'close') vm.settingsOpen = false;
    else store.deployments = [{ ...dep, id: 'wai-local' }];
    await flushPromises(); finish(plan); await pending; await flushPromises();
    expect(vm.settingsPlan).toBeNull(); expect(vm.settingsBusy).toBe(false);
    wrapper.unmount();
  });

  it('opens requested existing instance for failed-configuration review instead of resubmitting', async () => {
    const { store, wrapper, vm } = fixture(); await flushPromises(); vm.settingsOpen = false;
    store.openModelSettings('pony-local'); await flushPromises();
    expect(store.activeView).toBe('deployments'); expect(vm.settingsOpen).toBe(true);
    expect(vm.settingsSnapshot.id).toBe('pony-local'); expect(store.modelSettingsRequest).toBeNull();
    wrapper.unmount();
  });

  it('captures a fresh snapshot through the actual overview settings button', async () => {
    const { store, dep, wrapper, vm } = fixture(); await flushPromises();
    vm.settingsOpen = false; await flushPromises();
    store.deployments = [{ ...dep, instance_settings: { ...dep.instance_settings, policy_version: 8 } }]; await flushPromises();
    await wrapper.get('.model-detail button[title="打开实例设置"]').trigger('click');
    expect(vm.settingsSnapshot.instance_settings.policy_version).toBe(8); expect(vm.settingsStale).toBe(false);
    wrapper.unmount();
  });

  it('fetches the exact bound asset beyond the list limit and never substitutes another model', async () => {
    const { store, wrapper, vm } = fixture(); await flushPromises(); vm.settingsOpen = false;
    store.assets = store.assets.filter(item => item.id !== 'pony');
    let finish!: (value: any) => void;
    const request = vi.mocked(window.mediaCenterDesktop!.request);
    request.mockImplementation(() => new Promise(resolve => finish = resolve));
    vm.openSettings(vm.models[0]); await flushPromises();
    expect(vm.settings.base_asset_id).toBe('pony');
    expect(wrapper.get('.configuration-model-fields select').text()).toContain('当前绑定资产未同步');
    expect(request).toHaveBeenCalledWith('/api/v1/model-assets/pony', expect.anything());
    finish({ok:true,status:200,data:{id:'pony',display_name:'Pony from binding',media_kind:'image',role:'checkpoint',state:'ready',format:'safetensors',architecture_family:'sdxl'}});
    await flushPromises();
    expect(wrapper.get('.configuration-model-fields select').text()).toContain('Pony from binding');
    expect(vm.settings.base_asset_id).toBe('pony'); wrapper.unmount();
  });

  it('follows legal rollback branches and rejects terminal/backward operation transitions', () => {
    const op = (state: string) => ({id:'dop-1',deployment_id:'pony-local',state}) as any;
    expect(mergeDeploymentOperations([op('health_check')], [op('rollback')])[0].state).toBe('rollback');
    expect(mergeDeploymentOperations([op('rollback')], [op('failed')])[0].state).toBe('failed');
    expect(mergeDeploymentOperations([op('canceling')], [op('ready')])[0].state).toBe('canceling');
    expect(mergeDeploymentOperations([op('ready')], [op('failed')])[0].state).toBe('ready');
    expect(mergeDeploymentOperations([op('health_check')], [op('accepted')])[0].state).toBe('health_check');
  });

  it('retains the focused terminal result behind 100 other active operations', async () => {
    const { store, wrapper, vm, plan, edit } = fixture(); await edit();
    expect(store.focusedDeploymentId).toBe('pony-local');
    vm.settingsOperation = {id:'dop-focused',deployment_id:'pony-local',state:'accepted'};
    vm.settingsSubmitted = true;
    const others = Array.from({length:100}, (_, i) => ({id:`dop-${i}`,deployment_id:`instance-${i}`,state:'accepted'})) as any;
    store.deploymentOperations = mergeDeploymentOperations([...others, vm.settingsOperation],
      [{...vm.settingsOperation,state:'ready',payload:plan.operation}], store.focusedDeploymentId);
    await flushPromises();
    expect(store.deploymentOperations.length).toBe(101); expect(vm.currentSettingsOperation.state).toBe('ready');
    expect(vm.settingsApplying).toBe(false);
    wrapper.unmount(); expect(store.focusedDeploymentId).toBeNull();
  });

  it('does not suppress the second instance lookup while a shared-asset lookup is in flight', async () => {
    const { store, dep, wrapper, vm } = fixture(); await flushPromises(); vm.settingsOpen = false;
    store.assets = store.assets.filter(item => item.id !== 'pony');
    store.deployments.push({...dep,id:'pony-second'});
    const replies: Array<(value:any)=>void> = [];
    vi.mocked(window.mediaCenterDesktop!.request).mockImplementation(() => new Promise(resolve => replies.push(resolve)));
    vm.openSettings(vm.models.find((item:any) => item.id === 'pony-local')); await flushPromises();
    vm.openSettings(vm.models.find((item:any) => item.id === 'pony-second')); await flushPromises();
    expect(replies.length).toBe(2);
    const result = {ok:true,status:200,data:{id:'pony',display_name:'Pony exact asset',media_kind:'image',role:'checkpoint',state:'ready',format:'safetensors',architecture_family:'sdxl'}};
    replies[0](result); await flushPromises(); expect(vm.settingsBoundAssets).toEqual([]);
    replies[1](result); await flushPromises(); expect(vm.settingsBoundAssets[0].id).toBe('pony');
    wrapper.unmount();
  });
});

describe("model lifecycle projection", () => {
  const recipe = { id: "recipe-a", key: "recipe-a", state: "installed", model_id: "model-a", label: "Model A", kind: "image" };
  const deployment = { id: "dep-a", catalog_key: "recipe-a", model_id: "model-a", install_state: "ready", service_state: "ready", accepting_tasks: true, container_state: "running", configuration_state: "applied", actual_state: "loaded", asset_id: "asset-a" };

  it("joins recipe, deployment, installation and physical asset without duplicating ownership", () => {
    const [model] = buildCatalogModels([recipe], [deployment], [{ id: "ins-a", recipe_key: "recipe-a", updated_at: "2026-01-01" }], [{ id: "asset-a", state: "ready" }]);
    expect(model.deployments).toHaveLength(1);
    expect(model.installations).toHaveLength(1);
    expect(model.assets).toHaveLength(1);
    expect(isInstalled(model)).toBe(true);
    expect(isRunning(model)).toBe(true);
    expect(modelState(model)).toEqual({ label: "容器在线", tone: "ready" });
  });

  it("only allows uninstall after the service and runtime have stopped", () => {
    expect(canUninstall(deployment)).toBe(false);
    expect(canUninstall({ ...deployment, service_state: "stopped", actual_state: "unloaded", accepting_tasks: false })).toBe(true);
  });

  it("projects capacity admission as a blocking error instead of cold loading", () => {
    const [model] = buildCatalogModels([recipe], [{ ...deployment, service_state: "starting", actual_state: "waiting_runtime", container_state: "stopped", runtime_last_error: "gpu_capacity_unavailable" }], [], []);
    expect(modelState(model)).toEqual({ label: "GPU 容量不足", tone: "error" });
    expect(runtimeErrorLabel("gpu_capacity_unavailable")).toBe("GPU 容量不足");
    expect(runtimeErrorHint("gpu_capacity_unavailable")).toContain("没有足够的可调度显存");
    expect(runtimeErrorHint("residency_mode_unsupported")).toContain("只能使用按需策略");
  });

  it("projects every imported user deployment as its own manageable model", () => {
    const user = {
      ...deployment, id: "pony-local", catalog_key: "sdxl-single-file",
      model_id: "user/mdl_pony", label: "Pony Local", current_config_revision: 1,
      service_state: "stopped", accepting_tasks: false
    };
    const result = buildCatalogModels([
      { id: "sdxl-single-file", key: "sdxl-single-file", state: "available", model_id: "runtime/sdxl" }
    ], [user], [], [{ id: "asset-a", state: "ready", display_name: "Pony V6 XL" }]);
    const imported = result.find(item => item.key === "deployment:pony-local");
    expect(imported?.label).toBe("Pony Local");
    expect(imported?.deployments).toEqual([user]);
    expect(imported?.assets[0]?.display_name).toBe("Pony V6 XL");
    expect(isInstalled(imported!)).toBe(true);
  });
});

describe('deployment wizard asynchronous operation feedback', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    window.mediaCenterDesktop = {
      request: vi.fn(async () => ({ ok: true, status: 200, data: { items: [] } })),
      listModelUploadSessions: vi.fn(async () => []),
    } as any;
  });

  it.each([
    ['ready', null, '返回模型中心'],
    ['failed', 'recoverable', '重试'],
    ['failed', 'non_recoverable', '修改计划'],
    ['canceled', 'canceled', '重试'],
  ])('replaces accepted with SSE %s/%s and exposes %s', async (state, errorClass, action) => {
    const store = useAppStore();
    const wrapper = mount(ModelImportWizard, { props: { open: true }, global: { stubs: { Teleport: true } } });
    const vm = wrapper.vm as any;
    const accepted = { id: 'dop-1', deployment_id: 'wai-local', state: 'accepted', created_at: '2026-09-04T00:00:00Z' };
    vm.plan = { operation: { deployment_id: 'wai-local', gpu_uuids: ['GPU-0'], residency: 'on_demand' },
      runtime_profile: { label: 'SDXL', revision: 3, image_digest: 'sha256:fixture' }, capacity: { schedulable: true } };
    vm.operation = accepted;
    vm.step = 5;
    await flushPromises();
    expect(vm.currentOperation.state).toBe('accepted');
    expect(wrapper.findAll('button').some(button => button.text() === '取消')).toBe(true);
    store.deploymentOperations = [{ ...accepted, state, error_class: errorClass, updated_at: '2026-09-04T00:01:00Z' }] as any;
    await flushPromises();
    expect(vm.currentOperation.state).toBe(state);
    expect(wrapper.findAll('button').some(button => button.text() === '取消')).toBe(false);
    expect(wrapper.findAll('button').some(button => button.text() === action)).toBe(true);
    // An unrelated active operation cannot replace this deployment's result.
    store.deploymentOperations.push({ ...accepted, id: 'dop-other', deployment_id: 'pony-local' } as any);
    await flushPromises();
    expect(vm.currentOperation.state).toBe(state);
    wrapper.unmount();
  });

  function selectServer(store: ReturnType<typeof useAppStore>, id: string) {
    store.connections = { activeProfileId: id, revision: 1, profiles: ['server-a', 'server-b'].map(key =>
      ({ id: key, name: key, baseUrl: `http://${key}`, hasCredential: true })) } as any;
  }
  const uploadSession = { id: 'session-a', profileId: 'server-a', selection: 'file', displayName: 'WAI',
    state: 'selected', createdAt: '2026-09-04T00:00:00Z', files: [], preview: { role: 'checkpoint' } } as any;

  it('keeps paused upload in the inspection step without looking up an asset or reporting an error', async () => {
    const store = useAppStore(); selectServer(store, 'server-a');
    const paused = { ...uploadSession, state: 'paused', transferId: 'original-transfer' };
    window.mediaCenterDesktop!.startModelUpload = vi.fn(async () => ({ disposition: 'paused', session: paused })) as any;
    window.mediaCenterDesktop!.listModelUploadSessions = vi.fn(async () => [paused]) as any;
    const toast = vi.spyOn(store, 'toast');
    const wrapper = mount(ModelImportWizard, { props: { open: false } }), vm = wrapper.vm as any;
    vm.applySession(uploadSession); vm.form.revision = 'r1';
    await vm.publishAsset(); await flushPromises();
    expect(vm.step).toBe(2); expect(vm.asset).toBeNull(); expect(vm.session.state).toBe('paused');
    expect(window.mediaCenterDesktop!.request).not.toHaveBeenCalled();
    expect(toast.mock.calls.some(call => String(call[0]).includes('未返回模型资产'))).toBe(false);
    wrapper.unmount();
  });

  it('clears a server-owned plan synchronously and cannot send its commands to another server', async () => {
    const store = useAppStore(); selectServer(store, 'server-a');
    const wrapper = mount(ModelImportWizard, { props: { open: false } }), vm = wrapper.vm as any;
    vm.plan = { operation: { deployment_id: 'wai-local' }, capacity: { schedulable: true } };
    vm.operation = { id: 'dop-a', deployment_id: 'wai-local', state: 'accepted', payload: {} };
    vm.idempotencyKey = 'a-key'; vm.session = uploadSession;
    const create = vi.spyOn(store, 'createUserDeployment'), cancel = vi.spyOn(store, 'cancelDeploymentOperation');
    selectServer(store, 'server-b');
    expect(vm.plan).toBeNull(); expect(vm.session).toBeNull(); expect(vm.operation).toBeNull();
    expect(wrapper.emitted('close')).toHaveLength(1);
    await vm.commitDeployment(); await vm.cancelCurrentDeployment(); await vm.retryDeployment();
    expect(create).not.toHaveBeenCalled(); expect(cancel).not.toHaveBeenCalled();
    vm.resumeSession(uploadSession); expect(vm.session).toBeNull();
    wrapper.unmount();
  });

  it('discards late upload-session lists and file selection from the previous server', async () => {
    const store = useAppStore(); selectServer(store, 'server-a');
    let finishList!:(value:any)=>void, finishPick!:(value:any)=>void;
    window.mediaCenterDesktop!.listModelUploadSessions = vi.fn(() => new Promise(resolve => { finishList = resolve; })) as any;
    window.mediaCenterDesktop!.pickModelForImport = vi.fn(() => new Promise(resolve => { finishPick = resolve; })) as any;
    const wrapper = mount(ModelImportWizard, { props: { open: false } }), vm = wrapper.vm as any;
    const loading = vm.loadSessions(), picking = vm.chooseSource('file');
    selectServer(store, 'server-b');
    finishList([uploadSession]); finishPick({ session: uploadSession });
    await loading; await picking;
    expect(vm.sessions).toEqual([]); expect(vm.session).toBeNull(); expect(vm.loadingSessions).toBe(false);
    wrapper.unmount();
  });

  it('does not discard a previous-server session after delayed confirmation', async () => {
    const store = useAppStore(); selectServer(store, 'server-a');
    let finish!:(value:boolean)=>void;
    vi.spyOn(store, 'confirm').mockImplementation(() => new Promise(resolve => { finish = resolve; }));
    window.mediaCenterDesktop!.discardModelUpload = vi.fn() as any;
    const wrapper = mount(ModelImportWizard, { props: { open: false } });
    const discarding = (wrapper.vm as any).discardSession(uploadSession);
    selectServer(store, 'server-b'); finish(true); await discarding;
    expect(window.mediaCenterDesktop!.discardModelUpload).not.toHaveBeenCalled();
    wrapper.unmount();
  });

  it('does not land a late published-asset lookup into the new server draft', async () => {
    const store = useAppStore(); selectServer(store, 'server-a');
    window.mediaCenterDesktop!.startModelUpload = vi.fn(async () => ({ transfer: { asset_id: 'asset-a' } })) as any;
    let finish!:(value:any)=>void;
    window.mediaCenterDesktop!.request = vi.fn(() => new Promise(resolve => { finish = resolve; })) as any;
    const wrapper = mount(ModelImportWizard, { props: { open: false } }), vm = wrapper.vm as any;
    vm.applySession(uploadSession); vm.form.revision = 'r1';
    const publishing = vm.publishAsset(); await flushPromises();
    selectServer(store, 'server-b');
    finish({ ok: true, status: 200, data: { id: 'asset-a', role: 'checkpoint' } }); await publishing;
    expect(vm.asset).toBeNull(); expect(vm.step).toBe(1);
    wrapper.unmount();
  });
  it('keeps a completed upload recoverable when the asset read fails, without an unhandled rejection',async()=>{
    const store=useAppStore();selectServer(store,'server-a');
    vi.spyOn(store,'refreshSnapshot').mockResolvedValue(true);
    window.mediaCenterDesktop!.startModelUpload=vi.fn(async()=>({disposition:'uploaded',transfer:{asset_id:'asset-a'}})) as any;
    window.mediaCenterDesktop!.listModelUploadSessions=vi.fn(async()=>[{...uploadSession,state:'completed'}]) as any;
    window.mediaCenterDesktop!.request=vi.fn(async()=>({ok:false,status:503,data:{error:{message:'temporary asset read failure'}}})) as any;
    const wrapper=mount(ModelImportWizard,{props:{open:false}}),vm=wrapper.vm as any;
    vm.applySession(uploadSession);vm.form.revision='r1';
    await expect(vm.publishAsset()).resolves.toBeUndefined();
    expect(vm.asset).toBeNull();expect(vm.step).toBe(2);expect(vm.session.state).toBe('completed');
    expect(store.actionStates['model-import:publish:session-a']?.error).toContain('模型已入库，但读取详情失败');
    window.mediaCenterDesktop!.startModelUpload=vi.fn(async()=>({disposition:'reused',asset:{id:'asset-a',role:'vae',state:'ready'}})) as any;
    await vm.publishAsset();
    expect(vm.asset.id).toBe('asset-a');expect(vm.step).toBe(3);
    wrapper.unmount();
  });
});

describe('image model drafts and revision binding',()=>{
  beforeEach(()=>{
    setActivePinia(createPinia());
    vi.stubGlobal('ResizeObserver',class {observe(){} disconnect(){}});
    vi.stubGlobal('createImageBitmap',vi.fn(async()=>({width:32,height:32,close:vi.fn()})));
    URL.createObjectURL=vi.fn(()=>`blob:${crypto.randomUUID()}`);
    URL.revokeObjectURL=vi.fn();
    window.mediaCenterDesktop={request:vi.fn(async()=>({ok:true,status:200,data:{}})),uploadAsset:vi.fn(async()=>({id:'uploaded'}))} as any;
  });
  function fixture(){
    const store=useAppStore();
    store.connections={activeProfileId:'server-a',revision:1,profiles:[{id:'server-a',name:'A',baseUrl:'http://test',hasCredential:true}]} as any;
    const bind=(id:string)=>({model_key:'sdxl-single-file',recipe_revision:'profile-r1',model_asset_id:id,model_asset_revision:'r1',dependencies:[]});
    store.services=[{kind:'image',enabled:true,default_model:'wai',models:['wai','pony'].map(id=>({model_key:id,catalog_key:'sdxl-single-file',label:id,model_id:id,healthy:true,capabilities:{options:[{key:'seed',default:42,type:'integer'},{key:'width',default:1024,type:'integer'}]}}))}] as any;
    store.deployments=['wai','pony'].map(id=>({id,execution_binding:bind(id),configuration_binding:null}));
    store.assets=[{id:'shadow',revision:'r1',architecture_family:'sdxl',role:'lora',media_kind:'image',state:'ready',display_name:'Shadow'}];
    store.assetCompatibilities=['wai','pony'].map(id=>({subject_asset_id:'shadow',subject_revision:'r1',base_asset_id:id,base_revision:'r1',detector_version:'mc-sdxl-2',verdict:id==='wai'?'exact':'incompatible',reason_codes:id==='wai'?[]:['declared_base_identity_mismatch'],evidence_digest:id,created_at:''})) as any;
    return {store,wrapper:mount(ImageWorkspace),bind};
  }
  it('compares identities without object property-order dependence and scopes drafts by server',()=>{
    expect(sameModelIdentity({base:'a',vae:'b'},{vae:'b',base:'a'})).toBe(true);
    expect(sameModelIdentity({revision:'r1'},{revision:'r2'})).toBe(false);
    expect(imageDraftKey('a','wai')).not.toBe(imageDraftKey('b','wai'));
    expect(loraCompatibility([], {id:'lora',revision:'r1',state:'ready'},null)).toBeNull();
  });
  it('submits editable image text and unbounded parameters while hiding only explicitly fixed values',async()=>{
    const {store,wrapper}=fixture();
    store.services[0].models[0].capabilities!.options=[
      {key:'seed',label:'Seed',default:42,type:'integer'},
      {key:'width',label:'Width',default:1024,type:'integer',minimum:512,maximum:2048},
      {key:'steps',label:'Steps',default:25,type:'integer',advanced:true,minimum:1,maximum:50},
      {key:'negative_prompt',label:'Negative',default:'',type:'string',advanced:true},
      {key:'guidance_scale',label:'Fixed CFG',default:0,type:'number',minimum:0,maximum:0},
    ];
    await flushPromises();
    const field=(name:string)=>wrapper.findAll('.image-generator .option-fields label').find(label=>label.get('span').text()===name)!;
    expect(wrapper.findAll('.option-fields label')).toHaveLength(4);
    await field('Seed').get('input').setValue('24680');
    await field('Width').get('input').setValue('768');
    await field('Steps').get('input').setValue('12');
    await field('Negative').get('textarea').setValue('blurry, text');
    await wrapper.get('.image-generator-scroll textarea').setValue('parameter test');
    store.services=JSON.parse(JSON.stringify(store.services));
    await flushPromises();
    const submit=vi.spyOn(store,'submitTask').mockResolvedValue(null);
    await (wrapper.vm as any).submit();
    expect(submit).toHaveBeenCalledWith('image','wai','parameter test',expect.objectContaining({seed:24680,width:768,steps:12,negative_prompt:'blurry, text'}),undefined,undefined,expect.any(Object));
    wrapper.unmount();
  });
  it('keeps typed parameters through snapshot refresh, switching models and remount',async()=>{
    const {store,wrapper}=fixture();
    await wrapper.get('textarea').setValue('WAI draft');
    await wrapper.get('select[aria-label="任务 LoRA"]').setValue('shadow@r1');
    await wrapper.get('input[aria-label="LoRA 权重"]').setValue('0.65');
    store.services=structuredClone(JSON.parse(JSON.stringify(store.services)));
    await flushPromises();
    expect((wrapper.get('textarea').element as HTMLTextAreaElement).value).toBe('WAI draft');
    await wrapper.findAll('[role="option"]')[1].trigger('click');
    expect((wrapper.get('textarea').element as HTMLTextAreaElement).value).toBe('');
    expect(wrapper.get('select[aria-label="任务 LoRA"]').findAll('option')).toHaveLength(1);
    await wrapper.get('textarea').setValue('Pony draft');
    await wrapper.findAll('[role="option"]')[0].trigger('click');
    expect((wrapper.get('textarea').element as HTMLTextAreaElement).value).toBe('WAI draft');
    expect((wrapper.get('input[aria-label="LoRA 权重"]').element as HTMLInputElement).value).toBe('0.65');
    wrapper.unmount();
    const reopened=mount(ImageWorkspace);
    expect((reopened.get('textarea').element as HTMLTextAreaElement).value).toBe('WAI draft');
    reopened.unmount();
  });
  it('preserves but blocks stale configuration until explicitly adopted',async()=>{
    const {store,wrapper}=fixture();
    await wrapper.get('textarea').setValue('do not erase');
    store.deployments[0].execution_binding.recipe_revision='profile-r2';
    await flushPromises();
    expect(wrapper.text()).toContain('服务器配置已变化');
    expect((wrapper.get('textarea').element as HTMLTextAreaElement).value).toBe('do not erase');
    await wrapper.findAll('button').find(button=>button.text()==='采用当前配置')!.trigger('click');
    expect(wrapper.text()).not.toContain('服务器配置已变化');
    wrapper.unmount();
  });
  it('restores a different model history only with exact original assets and LoRA',async()=>{
    const {wrapper,bind}=fixture();
    await wrapper.findAll('[role="option"]')[1].trigger('click');
    await (wrapper.vm as any).reuse({model:'wai',prompt:'from history',options:{seed:99,width:768},execution_binding:bind('wai'),configuration_binding:null,loras:[{asset_id:'shadow',revision:'r1',family:'sdxl',weight:.55}]});
    await flushPromises();
    expect((wrapper.get('textarea').element as HTMLTextAreaElement).value).toBe('from history');
    expect((wrapper.get('input[aria-label="LoRA 权重"]').element as HTMLInputElement).value).toBe('0.55');
    await (wrapper.vm as any).reuse({model:'pony',prompt:'must reject',options:{seed:10},execution_binding:bind('pony'),configuration_binding:null,loras:[{asset_id:'shadow',revision:'r1',family:'sdxl',weight:.5}]});
    expect((wrapper.get('textarea').element as HTMLTextAreaElement).value).toBe('from history');
    wrapper.unmount();
  });
  function switchProfile(store:ReturnType<typeof useAppStore>,id:string){
    store.tasks=[];
    store.connections={...store.connections,activeProfileId:id,profiles:['server-a','server-b'].map(key=>({id:key,name:key,baseUrl:`http://${key}`,hasCredential:true}))} as any;
    store.connectionEpoch+=1;
  }
  it('keeps dirty task-backed canvases per server and cannot save or upscale A into empty B',async()=>{
    const {store,wrapper}=fixture(),vm=wrapper.vm as any;
    store.services[0].models.push({model_key:'upscale',healthy:true,capabilities:{workflow:'upscale'}} as any);
    const base=new Blob(['base']),edited=new Blob(['edited']);
    await vm.createDocument(vm.documentKey('task:same-id'),'A image',base,'same-id');
    await vm.replaceBlob(edited,'edited');
    expect(wrapper.find('.image-content img').attributes('alt')).toBe('A image');
    switchProfile(store,'server-b');await flushPromises();
    expect(wrapper.find('.image-content').exists()).toBe(false);
    await vm.saveDocument();await vm.upscale();
    expect(window.mediaCenterDesktop!.uploadAsset).not.toHaveBeenCalled();
    await vm.createDocument(vm.documentKey('task:same-id'),'B image',new Blob(['B']),'same-id');
    switchProfile(store,'server-a');await flushPromises();
    expect(vm.documentState.blob).toBe(edited);
    expect(vm.documentState.dirty).toBe(true);
    expect(vm.documentState.undo).toHaveLength(1);
    expect(wrapper.find('.image-content img').attributes('alt')).toBe('A image');
    wrapper.unmount();
  });
  it('does not start an upload after the server changes during blob encoding',async()=>{
    const {store,wrapper}=fixture(),vm=wrapper.vm as any;
    await vm.createDocument(vm.documentKey('task:a'),'A image',new Blob(['base']),'a');
    const edited=new Blob(['edited']);let finish!:(bytes:ArrayBuffer)=>void;
    Object.defineProperty(edited,'arrayBuffer',{value:()=>new Promise<ArrayBuffer>(resolve=>{finish=resolve})});
    await vm.replaceBlob(edited,'edited');
    const saving=vm.saveDocument();switchProfile(store,'server-b');finish(new ArrayBuffer(2));await saving;
    expect(window.mediaCenterDesktop!.uploadAsset).not.toHaveBeenCalled();
    wrapper.unmount();
  });
  it('does not submit an uploaded A image as a task after switching to B',async()=>{
    const {store,wrapper}=fixture(),vm=wrapper.vm as any;
    const edited=new Blob(['edited']);Object.defineProperty(edited,'arrayBuffer',{value:async()=>new ArrayBuffer(2)});
    await vm.createDocument(vm.documentKey('task:a'),'A image',new Blob(['base']),'a');
    await vm.replaceBlob(edited,'edited');
    let finish!:(asset:{id:string})=>void;
    window.mediaCenterDesktop!.uploadAsset=vi.fn(()=>new Promise(resolve=>{finish=resolve})) as any;
    const submit=vi.spyOn(store,'submitTask');
    const saving=vm.saveDocument();await flushPromises();
    expect(window.mediaCenterDesktop!.uploadAsset).toHaveBeenCalledOnce();
    switchProfile(store,'server-b');finish({id:'uploaded-to-a'});await saving;
    expect(submit).not.toHaveBeenCalled();
    wrapper.unmount();
  });
  it('discards a late decoded image and thumbnail from the previous server',async()=>{
    const {store,wrapper}=fixture(),vm=wrapper.vm as any;
    let finish!:(bitmap:any)=>void;
    vi.stubGlobal('createImageBitmap',vi.fn(()=>new Promise(resolve=>{finish=resolve})));
    const loading=vm.createDocument(vm.documentKey('task:a'),'A image',new Blob(['base']),'a');
    switchProfile(store,'server-b');finish({width:32,height:32,close:vi.fn()});await loading;
    expect(vm.documentState).toBeNull();
    let finishUrl!:(url:string)=>void;
    vi.spyOn(store,'fetchArtifactUrl').mockImplementation(()=>new Promise(resolve=>{finishUrl=resolve}));
    store.tasks=[{id:'b',service:'image',status:'succeeded',output:{artifact_url:'/artifacts/b.png'}}] as any;
    // Call directly to keep this assertion independent from Vue's scheduled watcher.
    const history=vm.loadHistory();switchProfile(store,'server-a');finishUrl('blob:late-b');await history;
    expect(Object.keys(vm.historyUrls)).toHaveLength(0);
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:late-b');
    wrapper.unmount();
  });
});
