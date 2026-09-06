import { beforeEach, describe, expect, it, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import StatusCenter from "./StatusCenter.vue";
import { useAppStore } from "@/stores/app";

describe("StatusCenter", () => {
  beforeEach(() => { localStorage.clear(); setActivePinia(createPinia()); });

  it('keeps timeout progress distinct from a user cancellation and retains the open panel', async () => {
    const store = useAppStore(); store.openStatusCenter = 'task';
    store.tasks = [{id:'timeout',service:'image',model:'ph8-wai',prompt:'courtyard',status:'cancel_requested',
      error:'task_timed_out',stage:'timeout_stopping'}];
    const wrapper = mount(StatusCenter, {props:{kind:'task'}});
    expect(wrapper.text()).toContain('超时 · 正在停止');
    expect(wrapper.text()).toContain('等待执行退出确认');
    expect(wrapper.text()).not.toContain('timeout_stopping');
    const panel = wrapper.get('.status-center-panel').element;
    store.tasks = [{...store.tasks[0]!,status:'failed'}]; await flushPromises();
    expect(wrapper.get('.status-center-panel').element).toBe(panel);
    expect(wrapper.text()).toContain('执行超时');
    expect(wrapper.text()).not.toContain('已取消'); wrapper.unmount();
  });

  it('keeps removal distinct from deployment and permits only the current failed owner to retry', async () => {
    const store = useAppStore();
    store.openStatusCenter = 'task';
    const operation = { id: 'dop-remove', deployment_id: 'wai', state: 'accepted',
      payload: { action: 'uninstall' }, updated_at: '2026-09-05T00:00:00Z' } as any;
    store.deploymentOperations = [operation];
    store.deployments = [{id: 'wai', removal_operation_id:'dop-remove'}];
    const wrapper = mount(StatusCenter, { props: {kind:'task'} });
    expect(wrapper.text()).toContain('正在移除容器');
    expect(wrapper.find('.deployment-status-row button').exists()).toBe(false);
    store.deploymentOperations = [{ ...operation, state:'failed', error_class:'recoverable', error_message:'退出确认失败' }];
    await flushPromises();
    const retry = vi.spyOn(store, 'retryUserRemoval').mockResolvedValue(null);
    const install = vi.spyOn(store, 'createUserDeployment');
    await wrapper.get('.deployment-status-row button').trigger('click');
    expect(retry).toHaveBeenCalledWith(store.deploymentOperations[0]);
    expect(install).not.toHaveBeenCalled();
    store.deployments = [{id:'wai', removal_operation_id:'new-owner'}];
    await flushPromises();
    expect(wrapper.get('.deployment-status-row button').attributes('disabled')).toBeDefined();
    store.deploymentOperations = [{...operation, state:'ready'}];
    await flushPromises();
    expect(wrapper.text()).toContain('已卸载 · 资产保留');
    expect(wrapper.find('.deployment-status-row button').exists()).toBe(false);
    wrapper.unmount();
  });

  it("keeps the message panel open while expanding a row", async () => {
    const store = useAppStore();
    store.statusMessages = [{ id: "m1", title: "生成完成", detail: "图片任务已完成", type: "info", createdAt: new Date().toISOString(), read: false, serverId: "server", serverName: "MediaCenter", taskId: null, service: "image" }];
    const wrapper = mount(StatusCenter, { props: { kind: "message" }, global: { plugins: [createPinia()] } });
    const mountedStore = useAppStore();
    mountedStore.statusMessages = store.statusMessages;
    await wrapper.get(".status-center-trigger").trigger("click");
    expect(wrapper.find(".status-center-panel").exists()).toBe(true);
    await wrapper.get(".message-main").trigger("click");
    expect(wrapper.find(".status-center-panel").exists()).toBe(true);
    expect(wrapper.find(".message-detail").text()).toContain("图片任务已完成");
    expect(mountedStore.statusMessages[0].read).toBe(true);
  });

  it.each(['accepted', 'preparing_runtime'])('recovers %s deployment controls from a snapshot after remount', async (state) => {
    const store = useAppStore();
    store.openStatusCenter = 'task';
    const operation = { id: 'dop-1', deployment_id: 'wai-local', state, created_at: '2026-09-04T00:00:00Z' } as any;
    store.deploymentOperations = [operation];
    let wrapper = mount(StatusCenter, { props: { kind: 'task' } });
    wrapper.unmount();
    wrapper = mount(StatusCenter, { props: { kind: 'task' } });
    expect(wrapper.text()).toContain('wai-local');
    expect(wrapper.text()).not.toContain('当前服务器还没有任务');
    const cancel = vi.spyOn(store, 'cancelDeploymentOperation').mockResolvedValue(null);
    await wrapper.get('.deployment-status-row button').trigger('click');
    expect(cancel).toHaveBeenCalledWith(operation);
    store.deploymentOperations = [{ ...operation, state: 'ready' }];
    await flushPromises();
    expect(wrapper.text()).toContain('部署完成');
    expect(wrapper.find('.deployment-status-row button').exists()).toBe(false);
    // Connection reset clears the server-scoped snapshot, even with the popover open.
    store.deploymentOperations = [];
    await flushPromises();
    expect(wrapper.text()).not.toContain('wai-local');
    wrapper.unmount();
  });

  it('keeps failure details and retries only recoverable operations with the same payload', async () => {
    const store = useAppStore();
    store.openStatusCenter = 'task';
    const operation = { id: 'dop-1', deployment_id: 'wai-local', state: 'failed', error_class: 'recoverable',
      error_message: '镜像准备中断', payload: { deployment_id: 'wai-local', runtime_profile_revision: 3 } } as any;
    store.deploymentOperations = [operation];
    const retry = vi.spyOn(store, 'createUserDeployment').mockResolvedValue(null);
    const wrapper = mount(StatusCenter, { props: { kind: 'task' } });
    expect(wrapper.text()).toContain('镜像准备中断');
    expect(wrapper.get('.deployment-status-row button').text()).toContain('重试');
    await wrapper.get('.deployment-status-row button').trigger('click');
    expect(retry).toHaveBeenCalledWith(operation.payload);
    store.deploymentOperations[0].error_class = 'non_recoverable';
    await flushPromises();
    expect(wrapper.find('.deployment-status-row button').exists()).toBe(false);
    wrapper.unmount();
  });

  it.each(['failed', 'canceled'])('routes %s configuration to current settings without replaying stale CAS', async state => {
    const store = useAppStore(); store.openStatusCenter = 'task';
    store.deploymentOperations = [{ id: 'dop-config', deployment_id: 'pony-local', state, error_class: 'recoverable',
      payload: { deployment_id: 'pony-local', expected_configuration: { config_revision: 1, config_digest: 'old', policy_version: 1 } } }] as any;
    const post = vi.spyOn(store, 'createUserDeployment');
    const wrapper = mount(StatusCenter, { props: { kind: 'task' } });
    expect(wrapper.get('.deployment-status-row button').text()).toContain('重新配置');
    await wrapper.get('.deployment-status-row button').trigger('click');
    expect(post).not.toHaveBeenCalled(); expect(store.modelSettingsRequest?.deploymentId).toBe('pony-local');
    expect(store.activeView).toBe('deployments'); wrapper.unmount();
  });
});
