import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import type { MediaTask } from "@/types/contracts";
import { useAppStore } from "@/stores/app";
import TaskMonitor from "./TaskMonitor.vue";

const task: MediaTask = { id: "task-timeout", version: 4, service: "video", model: "video-model", prompt: "courtyard",
  status: "cancel_requested", stage: "timeout_stopping", error: "task_timed_out",
  attempt: { id: "attempt-1", instance_id: "video-model", epoch: 1, status: "cancel_requested",
    exit_confirmed: 0, exit_evidence: null, execution_deadline_at: null, termination_reason: "task_timed_out" } };

describe("TaskMonitor timeout recovery", () => {
  beforeEach(() => { localStorage.clear(); setActivePinia(createPinia()); });
  it.each(['model_out_of_memory','adapter_reset_failed'])('keeps %s recovery disabled until confirmed, without replacing the control', async error => {
    const store = useAppStore(); const retry = vi.spyOn(store, 'retryTask').mockResolvedValue(null);
    const failed = { ...task, status: 'failed' as const, error, attempt: { ...task.attempt!, termination_reason: null } };
    const wrapper = mount(TaskMonitor, { props: {task: failed, kind: 'video'} });
    const button = wrapper.get('button[title="等待执行退出确认后重试"]');
    expect(button.attributes('disabled')).toBeDefined();
    expect(wrapper.text()).not.toContain(error);
    await button.trigger('click'); expect(retry).not.toHaveBeenCalled();
    const stopped = { ...failed, version: 5, attempt: { ...failed.attempt, exit_confirmed: 1 as const } };
    await wrapper.setProps({task: stopped}); await flushPromises();
    const enabled = wrapper.get('button[title="按原任务配置重试"]');
    expect(enabled.element).toBe(button.element);
    expect(enabled.attributes('disabled')).toBeUndefined();
    expect(wrapper.text()).toContain('执行已停止');
    await enabled.trigger('click'); expect(retry).toHaveBeenCalledWith(stopped);
    wrapper.unmount();
  });
  it("keeps the stop control visible but disabled and explains the pending exit", async () => {
    const store = useAppStore(); const cancel = vi.spyOn(store, "cancelTask");
    const wrapper = mount(TaskMonitor, { props: { task, kind: "video" } });
    expect(wrapper.text()).toContain("超时 · 正在停止");
    expect(wrapper.text()).not.toContain("timeout_stopping");
    const stop = wrapper.get('button[title="已请求停止，等待执行退出确认"]');
    expect(stop.attributes("disabled")).toBeDefined();
    await stop.trigger("click"); expect(cancel).not.toHaveBeenCalled();
    wrapper.unmount();
  });

  it("updates recovery in place only after the server confirms execution exit", async () => {
    const store = useAppStore(); const retry = vi.spyOn(store, "retryTask").mockResolvedValue(null);
    const failed = { ...task, status: "failed" as const };
    const wrapper = mount(TaskMonitor, { props: { task: failed, kind: "video" } });
    const button = wrapper.get('button[title="等待执行退出确认后重试"]');
    expect(button.attributes("disabled")).toBeDefined();
    expect(wrapper.text()).not.toContain("task_timed_out");
    const stopped = { ...failed, version: 5, attempt: { ...task.attempt!, exit_confirmed: 1 as const } };
    await wrapper.setProps({ task: stopped }); await flushPromises();
    const enabled = wrapper.get('button[title="按原任务配置重试"]');
    expect(enabled.element).toBe(button.element);
    expect(enabled.attributes("disabled")).toBeUndefined();
    await enabled.trigger("click"); expect(retry).toHaveBeenCalledWith(stopped);
    expect(wrapper.text()).toContain("执行已停止"); wrapper.unmount();
  });
});
