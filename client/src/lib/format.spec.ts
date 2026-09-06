import { describe, expect, it } from "vitest";
import type { MediaTask } from "@/types/contracts";
import { canCancelTask, taskErrorMessage, taskRetryDisabledReason, taskStageLabel, taskStatusLabel, taskTimedOut } from "./format";

const timeout: MediaTask = {
  id: "task-timeout", version: 5, service: "image", model: "ph8-wai", prompt: "courtyard",
  status: "cancel_requested", stage: "timeout_stopping", error: "task_timed_out",
  attempt: { id: "attempt-1", instance_id: "ph8-wai", epoch: 1, status: "cancel_requested",
    exit_confirmed: 0, exit_evidence: null, execution_deadline_at: "2026-09-05T00:00:10Z", termination_reason: "task_timed_out" },
};

describe("authoritative task feedback", () => {
  it("explains typed allocation failures without claiming a device or premature recovery", () => {
    const failed = { ...timeout, status: "failed" as const, error: "model_out_of_memory",
      attempt: { ...timeout.attempt!, termination_reason: null } };
    expect(taskStatusLabel(failed)).toBe("运行内存不足");
    expect(taskErrorMessage(failed)).toContain("等待执行退出确认");
    expect(taskErrorMessage(failed)).not.toContain("显存");
    expect(taskRetryDisabledReason(failed)).not.toBe("");
    const stopped = { ...failed, attempt: { ...failed.attempt, exit_confirmed: 1 as const } };
    expect(taskErrorMessage(stopped)).toContain("降低生成分辨率");
    expect(taskRetryDisabledReason(stopped)).toBe("");
    expect(taskErrorMessage({ ...stopped, error: "adapter_reset_failed" })).toContain("原执行环境已退出");
    expect(taskErrorMessage({ ...failed, error: "adapter_reset_failed" })).toContain("退出确认前不能重试");
    expect(taskStatusLabel({ ...failed, status: "queued" })).toBe("排队中");
    expect(taskStatusLabel({ ...failed, attempt: timeout.attempt })).toBe("执行超时");
  });

  it("does not confuse a timed-out stop with user cancellation or successful cleanup", () => {
    expect(taskStatusLabel(timeout)).toBe("超时 · 正在停止");
    expect(taskStageLabel(timeout)).toBe("等待执行退出确认");
    expect(taskErrorMessage(timeout)).toContain("退出确认后才能重试");
    expect(canCancelTask(timeout)).toBe(false);
    expect(taskRetryDisabledReason(timeout)).not.toBe("");
    const canceled = { ...timeout, error: null, attempt: { ...timeout.attempt!, termination_reason: "user_canceled" as const } };
    expect(taskTimedOut(canceled)).toBe(false);
    expect(taskStatusLabel(canceled)).toBe("取消中");
  });

  it("waits for exact exit proof and a current task version before enabling retry", () => {
    const failed = { ...timeout, status: "failed" as const };
    expect(taskRetryDisabledReason(failed)).toBe("等待执行退出确认后重试");
    const stopped = { ...failed, attempt: { ...failed.attempt!, exit_confirmed: 1 as const } };
    expect(taskRetryDisabledReason(stopped)).toBe("");
    expect(taskStatusLabel(stopped)).toBe("执行超时");
    expect(taskStageLabel(stopped)).toBe("执行已停止");
    expect(taskErrorMessage(stopped)).toContain("可调整参数或服务时限后重试");
    expect(taskRetryDisabledReason({ ...stopped, version: undefined })).toContain("任务版本");
    expect(taskRetryDisabledReason({ ...stopped, version: 1.5 })).toContain("任务版本");
    expect(taskRetryDisabledReason({ ...stopped, attempt: null })).toBe("");
  });

  it.each(["queued", "assigned", "running", "succeeded"] as const)("does not label %s from the previous timed-out attempt", status => {
    const next = { ...timeout, status, stage: status, error: null };
    expect(taskTimedOut(next)).toBe(false);
    expect(taskStatusLabel(next)).not.toContain("超时");
    expect(taskStageLabel(next)).toBe(status);
  });

  it("does not invent a timeout from an elapsed deadline or browser clock", () => {
    expect(taskTimedOut({ ...timeout, error: null, attempt: { ...timeout.attempt!, termination_reason: null } })).toBe(false);
    expect(taskStatusLabel({ ...timeout, status: "failed", attempt: null })).toBe("执行超时");
  });
});
