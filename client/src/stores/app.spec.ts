import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useAppStore } from "./app";

function bridge() {
  const listeners: Array<(event: any) => void> = [];
  return {
    listeners,
    listConnections: vi.fn(async () => ({
      activeProfileId: "p1",
      revision: 1,
      profiles: [
        {
          id: "p1",
          name: "Server",
          baseUrl: "http://10.0.0.1:8787",
          hasCredential: true,
        },
      ],
    })),
    startEvents: vi.fn(async () => ({})),
    stopEvents: vi.fn(async () => undefined),
    onServerEvent: vi.fn((callback: any) => {
      listeners.push(callback);
      return () => undefined;
    }),
    onServerEventStatus: vi.fn(() => () => undefined),
    onModelTransferProgress: vi.fn(() => () => undefined),
    onNavigate: vi.fn(() => () => undefined),
    onRefresh: vi.fn(() => () => undefined),
    request: vi.fn(async (path: string) => {
      const data: Record<string, any> = {
        "/api/v1/overview": {
          services_online: 1,
          active_tasks: 0,
          completed_tasks: 0,
          failed_tasks: 0,
        },
        "/api/v1/services": {
          items: [
            {
              kind: "image",
              name: "图片",
              enabled: true,
              available: true,
              models: [],
            },
          ],
        },
        "/api/v1/tasks?limit=50": { items: [] },
        "/api/v1/deployments": { items: [] },
        "/api/v1/service-catalog": { items: [] },
        "/api/v1/service-installations?limit=100": { items: [] },
        "/api/v1/resources/gpus": { configured_gpu_indices: [], gpus: [] },
        "/api/v1/model-assets?limit=500": { items: [] },
        "/api/v1/model-transfers?limit=100": { items: [] },
        "/api/v1/runtime-profiles": { items: [] },
        "/api/v1/deployment-operations?limit=100": { items: [] },
        "/api/v1/asset-compatibility": { items: [] },
      };
      return { ok: true, status: 200, data: data[path] };
    }),
    connectServer: vi.fn(),
    switchServer: vi.fn(),
    logoutServer: vi.fn(),
    removeServer: vi.fn(),
    configureSourceAuthorization: vi.fn(),
    openSourceTerms: vi.fn(),
    fetchArtifact: vi.fn(),
    saveArtifact: vi.fn(),
    uploadAsset: vi.fn(),
    pickAssets: vi.fn(),
    pickAndUploadModel: vi.fn(),
    pickModelForImport: vi.fn(),
    listModelUploadSessions: vi.fn(async () => []),
    startModelUpload: vi.fn(),
    resumeModelUpload: vi.fn(),
    pauseModelUpload: vi.fn(),
    discardModelUpload: vi.fn(),
    notifyTask: vi.fn(async () => true),
    windowControl: vi.fn(),
    onWindowState: vi.fn(),
  };
}

describe("app store", () => {
  beforeEach(() => {
    localStorage.clear();
    setActivePinia(createPinia());
  });
  it('retries with the newest server task version and rejects an unconfirmed exit', async () => {
    const mock = bridge(); window.mediaCenterDesktop = mock as any;
    const store = useAppStore(); await store.initialize(); mock.request.mockClear();
    const old = {id:'timed-out',service:'image',model:'ph8-wai',prompt:'courtyard',status:'failed',version:4,
      error:'task_timed_out',attempt:{exit_confirmed:0}} as any;
    store.tasks = [old];
    expect(await store.retryTask(old)).toBeNull(); expect(mock.request).not.toHaveBeenCalled();
    expect(store.statusMessages[0]?.detail).toContain('等待执行退出确认');
    store.tasks = [{...old,version:5,attempt:{...old.attempt,exit_confirmed:1}}];
    mock.request.mockResolvedValue({ok:true,status:200,data:{...old,status:'queued',version:6,error:null}});
    await store.retryTask(old);
    expect(mock.request).toHaveBeenCalledWith('/api/v1/tasks/timed-out/retry',expect.objectContaining({method:'POST',body:JSON.stringify({version:5})}));
    expect(store.tasks[0]?.status).toBe('queued'); expect(store.selectedTaskIds.image).toBe('timed-out');
  });

  it('opens the exact task from the status center instead of retaining an older selected image', () => {
    const store = useAppStore();
    const old = {id:'history',service:'image',model:'ph8-wai',prompt:'old image',status:'succeeded'} as any;
    const current = {...old,id:'timeout',status:'failed',error:'task_timed_out'};
    store.tasks = [current,old]; store.selectedTaskIds.image = old.id;
    store.openTask(current.id);
    expect(store.selectedTask('image')?.id).toBe(current.id);
    expect(store.activeView).toBe('image');
  });

  it('does not let a delayed snapshot replace a retried task or erase a task created since the read', async () => {
    const mock = bridge(); window.mediaCenterDesktop = mock as any;
    const store = useAppStore(); await store.initialize();
    const old = {id:'retry-race',service:'image',model:'ph8-wai',prompt:'courtyard',status:'failed',version:4,
      error:'task_timed_out',attempt:{exit_confirmed:1}} as any;
    store.tasks = [old];
    const original = mock.request.getMockImplementation()!;
    let finish!:(result:any)=>void;
    mock.request.mockImplementation(path => {
      if(path === '/api/v1/tasks?limit=50') return new Promise(resolve=>{finish=resolve;});
      if(path.endsWith('/retry')) return Promise.resolve({ok:true,status:200,data:{...old,status:'queued',version:5,error:null}});
      return original(path);
    });
    const snapshot = store.refreshSnapshot();
    await store.retryTask(old);
    store.tasks.unshift({...old,id:'created-after-read',status:'queued',version:1,attempt:null,error:null});
    finish({ok:true,status:200,data:{items:[old]}}); await snapshot;
    expect(store.tasks.find(item=>item.id==='retry-race')?.status).toBe('queued');
    expect(store.tasks.find(item=>item.id==='retry-race')?.version).toBe(5);
    expect(store.tasks.some(item=>item.id==='created-after-read')).toBe(true);
    expect(store.statusMessages.some(item=>item.title.includes('执行超时'))).toBe(false);
  });

  it.each(['cancel_requested','failed'])('does not submit a stale cancel after the task changes to %s in the dialog', async status => {
    const mock = bridge(); window.mediaCenterDesktop = mock as any;
    const store = useAppStore(); await store.initialize(); mock.request.mockClear();
    const task = {id:'race',service:'image',model:'ph8-wai',prompt:'courtyard',status:'running',version:3} as any;
    store.tasks = [task]; const pending = store.cancelTask(task);
    store.tasks = [{...task,status}]; store.answerConfirmation(true);
    expect(await pending).toBe(false); expect(mock.request).not.toHaveBeenCalled();
    expect(await store.cancelTask(store.tasks[0]!)).toBe(false);
  });

  it('records one timeout-stopping message and a separate confirmed failure without duplicate snapshot messages', async () => {
    const mock = bridge(); window.mediaCenterDesktop = mock as any;
    const original = mock.request.getMockImplementation()!;
    let task = {id:'timeout',service:'image',model:'ph8-wai',prompt:'courtyard',status:'running',version:3} as any;
    mock.request.mockImplementation(path => path === '/api/v1/tasks?limit=50'
      ? Promise.resolve({ok:true,status:200,data:{items:[task]}}) : original(path));
    const store = useAppStore(); await store.initialize();
    task = {...task,status:'cancel_requested',version:4,error:'task_timed_out'};
    await store.refreshSnapshot(); await store.refreshSnapshot();
    expect(store.statusMessages.filter(item=>item.title.includes('任务超时'))).toHaveLength(1);
    expect(store.statusMessages[0]?.detail).toContain('退出确认后才能重试');
    expect(mock.notifyTask).not.toHaveBeenCalled();
    task = {...task,status:'failed',version:5,attempt:{exit_confirmed:1,termination_reason:'task_timed_out'}};
    await store.refreshSnapshot(); await store.refreshSnapshot();
    expect(store.statusMessages.filter(item=>item.title.includes('任务执行超时'))).toHaveLength(1);
    expect(store.statusMessages[0]?.detail).toContain('执行已停止');
    expect(mock.notifyTask).toHaveBeenCalledOnce();
  });
  it('discards in-flight compatibility and generation responses after server changes',async()=>{
    const mock=bridge();window.mediaCenterDesktop=mock as any;
    const store=useAppStore();await store.initialize();
    let resolveCompatibility:(value:any)=>void=()=>{};
    mock.request.mockImplementation(()=>new Promise(resolve=>{resolveCompatibility=resolve;}));
    const pending=store.assessAssetCompatibility('lora','wai');
    store.connectionEpoch++;
    resolveCompatibility({ok:true,status:200,data:{subject_asset_id:'lora',base_asset_id:'wai',verdict:'exact'}});
    expect(await pending).toBeNull();expect(store.assetCompatibilities).toEqual([]);
    let resolveTask:(value:any)=>void=()=>{};
    mock.request.mockImplementation(()=>new Promise(resolve=>{resolveTask=resolve;}));
    const submitted=store.submitTask('image','wai','prompt',{});
    store.connectionEpoch++;
    resolveTask({ok:true,status:200,data:{id:'foreign-task'}});
    expect(await submitted).toBeNull();expect(store.tasks).toEqual([]);
    expect(store.statusMessages.some(item=>item.title.includes('foreign-task'))).toBe(false);
  });
  it('submits imported-model removal to its instance route and keeps accepted distinct from ready', async () => {
    const mock = bridge(); window.mediaCenterDesktop = mock as any;
    const store = useAppStore(); await store.initialize();
    const instance = {id:'wai-local', incarnation:'inc-wai', configuration_binding:{config_revision:3,config_digest:'c'.repeat(64)},
      instance_settings:{policy_version:7}, removal_operation_id:null};
    const recipe = {id:'wai-local',key:'deployment:wai-local',label:'WAI',user_imported:true,deployments:[instance]};
    const original = mock.request.getMockImplementation()!;
    mock.request.mockImplementation(async path => path.endsWith('/uninstall')
      ? {ok:true,status:202,data:{id:'dop-remove',deployment_id:'wai-local',state:'accepted',payload:{action:'uninstall'}}}
      : original(path));
    const pending = store.uninstallRecipe(recipe);
    store.answerConfirmation(true);
    const result = await pending;
    expect(result?.state).toBe('accepted');
    const call = mock.request.mock.calls.find(([path]) => path.endsWith('/uninstall')) as any;
    expect(call[0]).toBe('/api/v1/deployments/wai-local/uninstall');
    expect(JSON.parse(call[1].body)).toEqual({incarnation:'inc-wai',config_revision:3,config_digest:'c'.repeat(64),policy_version:7,retry_of:null});
    expect(call[1].idempotencyKey).toMatch(/^uninstall-/);
    expect(store.deploymentOperations.some(item => item.id === 'dop-remove')).toBe(true);
    expect(store.statusMessages.some(item => item.title.includes('卸载已进入后台'))).toBe(true);
    expect(store.statusMessages.some(item => item.title.includes('服务已卸载'))).toBe(false);
    const switched = store.uninstallRecipe(recipe);
    store.connectionEpoch++;
    store.answerConfirmation(true);
    expect(await switched).toBeNull();
    expect(mock.request.mock.calls.filter(([path]) => path.endsWith('/uninstall'))).toHaveLength(1);
  });

  it.each(['ready','failed'])('retains earlier %s removal evidence when accepted returns late and refresh fails', async state => {
    const mock = bridge(); window.mediaCenterDesktop = mock as any;
    const store = useAppStore(); await store.initialize();
    const instance = {id:'wai-local',incarnation:'inc-wai',install_state:'ready',configuration_binding:{config_revision:3,config_digest:'a'.repeat(64)},
      instance_settings:{policy_version:7},removal_operation_id:null};
    store.deployments = [instance];
    let finish!:(value:any)=>void;
    mock.request.mockImplementation(path => path.endsWith('/uninstall') ? new Promise(resolve=>finish=resolve)
      : Promise.resolve({ok:false,status:503,data:{error:{message:'fixture snapshot unavailable'}}}));
    const pending = store.uninstallRecipe({id:'wai-local',key:'deployment:wai-local',label:'WAI',user_imported:true,deployments:[instance]});
    store.answerConfirmation(true);
    await Promise.resolve(); await Promise.resolve();
    store.deploymentOperations = [{id:'dop-remove',deployment_id:'wai-local',state,payload:{action:'uninstall'}}] as any;
    finish({ok:true,status:202,data:{id:'dop-remove',deployment_id:'wai-local',state:'accepted',payload:{action:'uninstall'}}});
    expect((await pending)?.state).toBe(state);
    expect(store.deployments[0].removal_operation_state).toBe(state);
    expect(store.deployments[0].removal_operation_id).toBe(state==='ready'?null:'dop-remove');
    expect(store.deployments[0].install_state).toBe(state==='ready'?'configured':'ready');
  });

  it("loads one bounded snapshot and leaves subsequent synchronization to SSE", async () => {
    const mock = bridge();
    window.mediaCenterDesktop = mock as any;
    const store = useAppStore();
    await store.initialize();
    expect(store.authenticated).toBe(true);
    expect(store.overview.services_online).toBe(1);
    expect(mock.request).toHaveBeenCalledTimes(12);
    expect(mock.onServerEvent).toHaveBeenCalledOnce();
  });

  it("ignores foreign, duplicate and stale SSE resource versions", async () => {
    const mock = bridge();
    window.mediaCenterDesktop = mock as any;
    const store = useAppStore();
    await store.initialize();
    mock.request.mockClear();
    mock.request.mockImplementation(async (path: string) => ({
      ok: true,
      status: 200,
      data:
        path === "/api/v1/tasks/task-1"
          ? {
              id: "task-1",
              version: 2,
              service: "image",
              model: "sdxl",
              prompt: "test",
              status: "running",
              progress: 0.5,
            }
          : { items: [] },
    }));
    vi.useFakeTimers();
    const emit = (version: number, profileId = "p1", revision = 1) =>
      mock.listeners[0]({
        profileId,
        revision,
        event: {
          protocol: "mc.client/1",
          type: "task.changed",
          resource_id: "task-1",
          version,
          data: {},
        },
      });
    emit(2);
    await vi.advanceTimersByTimeAsync(100);
    expect(mock.request).toHaveBeenCalledTimes(1);
    emit(2);
    emit(1);
    emit(3, "foreign");
    emit(3, "p1", 2);
    await vi.advanceTimersByTimeAsync(200);
    expect(mock.request).toHaveBeenCalledTimes(1);
    expect(store.tasks[0]?.version).toBe(2);
    vi.useRealTimers();
  });

  it("applies volatile GPU telemetry without requesting a full snapshot", async () => {
    const mock = bridge();
    window.mediaCenterDesktop = mock as any;
    const store = useAppStore();
    await store.initialize();
    mock.request.mockClear();
    mock.listeners[0]({
      profileId: "p1",
      revision: 1,
      event: {
        protocol: "mc.client/1",
        type: "gpu.telemetry",
        resource_id: "gpus",
        occurred_at: 1_788_500_000,
        data: {
          telemetry_available: true,
          gpus: [{ index: 0, memory_total_mib: 49140, memory_free_mib: 48178 }],
        },
      },
    });
    expect(store.gpuResources?.gpus[0].memory_free_mib).toBe(48178);
    expect(mock.request).not.toHaveBeenCalled();
  });

  it("applies deployment-operation SSE as one local resource update", async () => {
    const mock = bridge();
    window.mediaCenterDesktop = mock as any;
    const store = useAppStore();
    await store.initialize();
    mock.request.mockClear();
    mock.request.mockImplementation(async (path: string) => ({
      ok: true,
      status: 200,
      data:
        path === "/api/v1/deployment-operations/dop-1"
          ? {
              id: "dop-1",
              deployment_id: "pony-local",
              state: "creating_container",
              plan_digest: "abc",
              version: 2,
            }
          : { items: [] },
    }));
    vi.useFakeTimers();
    mock.listeners[0]({
      profileId: "p1",
      revision: 1,
      event: {
        protocol: "mc.client/1",
        type: "deployment-operation.changed",
        resource_id: "dop-1",
        version: 2,
        data: {},
      },
    });
    await vi.advanceTimersByTimeAsync(100);
    expect(mock.request).toHaveBeenCalledWith(
      "/api/v1/deployment-operations/dop-1",
      expect.anything(),
    );
    expect(store.deploymentOperations[0]?.state).toBe("creating_container");
    expect(store.backgroundCount).toBe(1);
    vi.useRealTimers();
  });

  it('refreshes removal retry state from SSE without polling and ignores duplicate or foreign events', async () => {
    const mock = bridge(); window.mediaCenterDesktop = mock as any;
    const store = useAppStore(); await store.initialize();
    store.deployments = [{id:'pony-local',removal_operation_id:'dop-remove',removal_operation_state:'accepted'}];
    mock.request.mockClear();
    mock.request.mockImplementation(async path => ({ok:true,status:200,data:
      path === '/api/v1/deployment-operations/dop-remove'
        ? {id:'dop-remove',deployment_id:'pony-local',state:'failed',payload:{action:'uninstall'}}
        : {items:[{id:'pony-local',removal_operation_id:'dop-remove',removal_operation_state:'failed'}]}}));
    vi.useFakeTimers();
    try {
      const emit = (version:number, profileId='p1') => mock.listeners[0]({profileId,revision:1,event:{
        protocol:'mc.client/1',type:'deployment-operation.changed',resource_id:'dop-remove',version,data:{}}});
      emit(2); await vi.advanceTimersByTimeAsync(100);
      expect(store.deployments[0].removal_operation_state).toBe('failed');
      expect(store.deploymentOperations[0].state).toBe('failed');
      expect(mock.request.mock.calls.map(([path])=>path)).toEqual([
        '/api/v1/deployment-operations/dop-remove','/api/v1/deployments']);
      emit(2); emit(1); emit(3,'foreign'); await vi.advanceTimersByTimeAsync(1000);
      expect(mock.request).toHaveBeenCalledTimes(2);
    } finally { vi.useRealTimers(); }
  });

  it('does not apply a removal projection after switching server during its GET', async () => {
    const mock = bridge(); window.mediaCenterDesktop = mock as any;
    const store = useAppStore(); await store.initialize();
    let finish!:(value:any)=>void;
    mock.request.mockImplementation(path => path === '/api/v1/deployments'
      ? new Promise(resolve=>finish=resolve)
      : Promise.resolve({ok:true,status:200,data:{id:'dop-remove',deployment_id:'pony-local',state:'failed',payload:{action:'uninstall'}}}));
    vi.useFakeTimers();
    try {
      mock.listeners[0]({profileId:'p1',revision:1,event:{protocol:'mc.client/1',
        type:'deployment-operation.changed',resource_id:'dop-remove',version:2,data:{}}});
      await vi.advanceTimersByTimeAsync(100);
      store.connectionEpoch++;
      store.deployments = [{id:'different-server-model'}];
      finish({ok:true,status:200,data:{items:[{id:'pony-local',removal_operation_id:'dop-remove'}]}});
      await vi.advanceTimersByTimeAsync(1);
      expect(store.deployments).toEqual([{id:'different-server-model'}]);
    } finally { vi.useRealTimers(); }
  });

  it('does not let an older full snapshot erase the removal retry projection', async () => {
    const mock=bridge(); window.mediaCenterDesktop=mock as any;
    const store=useAppStore(); await store.initialize();
    const original=mock.request.getMockImplementation()!;
    let finish!:(value:any)=>void;
    let deploymentReads=0;
    mock.request.mockImplementation(path => {
      if(path==='/api/v1/deployments') {
        if(++deploymentReads===1)return new Promise(resolve=>finish=resolve);
        return Promise.resolve({ok:true,status:200,data:{items:[{id:'pony-local',removal_operation_id:'dop-remove',removal_operation_state:'failed'}]}});
      }
      if(path==='/api/v1/deployment-operations/dop-remove')return Promise.resolve({ok:true,status:200,
        data:{id:'dop-remove',deployment_id:'pony-local',state:'failed',payload:{action:'uninstall'}}});
      return original(path);
    });
    const snapshot=store.refreshSnapshot(false);
    vi.useFakeTimers();
    try {
      mock.listeners[0]({profileId:'p1',revision:1,event:{protocol:'mc.client/1',
        type:'deployment-operation.changed',resource_id:'dop-remove',version:2,data:{}}});
      await vi.advanceTimersByTimeAsync(100);
      expect(store.deployments[0].removal_operation_state).toBe('failed');
      finish({ok:true,status:200,data:{items:[{id:'pony-local',removal_operation_id:null}]}});
      await snapshot;
      expect(store.deployments[0].removal_operation_state).toBe('failed');
      expect(store.deployments[0].removal_operation_id).toBe('dop-remove');
    } finally { vi.useRealTimers(); }
  });

  it('does not regress a terminal operation when a same-server GET arrives late', async () => {
    const mock = bridge(); window.mediaCenterDesktop = mock as any;
    const store = useAppStore(); await store.initialize();
    const replies: Array<(value: any) => void> = [];
    mock.request.mockImplementation(() => new Promise(resolve => replies.push(resolve)));
    vi.useFakeTimers();
    try {
      for (const version of [1, 2]) {
        mock.listeners[0]({ profileId: 'p1', revision: 1, event: { protocol: 'mc.client/1', type: 'deployment-operation.changed', resource_id: 'dop-1', version, data: {} } });
        await vi.advanceTimersByTimeAsync(100);
      }
      expect(replies.length).toBe(2);
      replies[1]({ok:true,status:200,data:{id:'dop-1',deployment_id:'pony-local',state:'ready'}});
      await vi.advanceTimersByTimeAsync(1);
      replies[0]({ok:true,status:200,data:{id:'dop-1',deployment_id:'pony-local',state:'preparing_runtime'}});
      await vi.advanceTimersByTimeAsync(1);
      expect(store.deploymentOperations[0].state).toBe('ready');
      expect(store.backgroundCount).toBe(0);
    } finally { vi.useRealTimers(); }
  });

  it('retains terminal SSE that beats the accepted POST response', async () => {
    const mock = bridge(); window.mediaCenterDesktop = mock as any;
    const store = useAppStore(); await store.initialize();
    let finish!: (value: any) => void;
    mock.request.mockImplementation(() => new Promise(resolve => finish = resolve));
    const pending = store.createUserDeployment({deployment_id:'pony-local',expected_configuration:{config_revision:1}}, 'same-key');
    store.deploymentOperations = [{id:'dop-1',deployment_id:'pony-local',state:'ready'}] as any;
    finish({ok:true,status:202,data:{id:'dop-1',deployment_id:'pony-local',state:'accepted'}});
    expect((await pending)?.state).toBe('ready');
    expect(store.deploymentOperations[0].state).toBe('ready');
    expect(store.statusMessages[0].title).toBe('实例配置已生效');
  });

  it('does not regress ready or drop a just-accepted operation when an old snapshot arrives', async () => {
    const mock = bridge(); window.mediaCenterDesktop = mock as any;
    const store = useAppStore(); await store.initialize();
    const original = mock.request.getMockImplementation()!;
    let finish!: (value: any) => void;
    mock.request.mockImplementation(path => path === '/api/v1/deployment-operations?limit=100'
      ? new Promise(resolve => finish = resolve) : original(path));
    const pending = store.refreshSnapshot(false);
    store.deploymentOperations = [{id:'dop-1',deployment_id:'pony-local',state:'ready'},
      {id:'dop-new',deployment_id:'wai-local',state:'accepted'}] as any;
    finish({ok:true,status:200,data:{items:[{id:'dop-1',deployment_id:'pony-local',state:'creating_container'}]}});
    await pending;
    expect(store.deploymentOperations.find(item => item.id === 'dop-1')?.state).toBe('ready');
    expect(store.deploymentOperations.find(item => item.id === 'dop-new')?.state).toBe('accepted');
  });
});
