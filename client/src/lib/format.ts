import type { MediaTask } from "@/types/contracts";

export const mediaNames = { image: "图片生成", video: "视频生成", speech: "语音生成", music: "音乐合成" } as const;

export const statusNames: Record<string, string> = {
  queued: "排队中", assigned: "调度中", running: "执行中", cancel_requested: "取消中",
  succeeded: "已完成", failed: "失败", canceled: "已取消", interrupted: "执行中断"
};

// Use the server's termination cause, never a renderer timer. A retried queued
// task can still reference its previous attempt until the next dispatch.
export function taskTimedOut(task: MediaTask): boolean {
  return ["cancel_requested", "failed"].includes(task.status) &&
    (task.attempt?.termination_reason === "task_timed_out" || task.error === "task_timed_out");
}

export function taskStatusLabel(task: MediaTask): string {
  if (taskTimedOut(task)) return task.status === "cancel_requested" ? "超时 · 正在停止" : "执行超时";
  if (task.status === "failed" && task.error === "model_out_of_memory") return "运行内存不足";
  if (task.status === "failed" && task.error === "adapter_reset_failed") return "模型清理失败";
  return statusNames[task.status] ?? task.status;
}

export function taskStageLabel(task: MediaTask): string {
  if (taskTimedOut(task)) return task.attempt?.exit_confirmed === 1 ? "执行已停止" : "等待执行退出确认";
  if (task.status === "cancel_requested") return "等待执行退出确认";
  if (task.status === "failed" && ["model_out_of_memory", "adapter_reset_failed"].includes(task.error ?? ""))
    return task.attempt?.exit_confirmed === 1 ? "执行已停止" : "等待执行退出确认";
  return task.stage || "等待调度";
}

export function taskErrorMessage(task: MediaTask): string {
  if (taskTimedOut(task)) return task.attempt?.exit_confirmed === 1
    ? "任务超过服务端执行时限，执行已停止。可调整参数或服务时限后重试。"
    : "任务超过服务端执行时限，正在停止执行；退出确认后才能重试。";
  if (task.status === "failed" && task.error === "model_out_of_memory") return task.attempt?.exit_confirmed === 1
    ? "模型执行内存不足，执行已停止。可降低生成分辨率或调整部署资源后重试。"
    : "模型执行内存不足；等待执行退出确认后，降低生成分辨率或调整部署资源再重试。";
  if (task.status === "failed" && task.error === "adapter_reset_failed") return task.attempt?.exit_confirmed === 1
    ? "模型清理失败，原执行环境已退出。重试将使用重新准备的执行环境。"
    : "模型清理失败，执行环境正在隔离；退出确认前不能重试。";
  return task.error || "服务器未返回错误详情";
}

export function canCancelTask(task: MediaTask): boolean {
  return ["queued", "assigned", "running"].includes(task.status);
}

export function taskRetryDisabledReason(task: MediaTask): string {
  if (!["failed", "canceled", "interrupted"].includes(task.status)) return "任务尚未结束";
  if (task.attempt === undefined) return "尚未取得执行状态，请同步后重试";
  if (task.attempt && task.attempt.exit_confirmed !== 1) return "等待执行退出确认后重试";
  if (!Number.isInteger(task.version) || Number(task.version) < 1) return "尚未取得任务版本，请同步后重试";
  return "";
}

export function formatDate(value?: string): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(date);
}

export function humanBytes(value?: number | null): string {
  if (value == null || !Number.isFinite(value)) return "—";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let size = Math.max(0, value); let index = 0;
  while (size >= 1024 && index < units.length - 1) { size /= 1024; index += 1; }
  return `${size.toFixed(index > 1 ? 1 : 0)} ${units[index]}`;
}

export function humanDuration(seconds?: number | null): string {
  if (seconds == null || !Number.isFinite(seconds)) return "—";
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)} 秒`;
  return `${Math.floor(seconds / 60)} 分 ${Math.round(seconds % 60)} 秒`;
}

export function taskElapsed(task: { started_at?: string; created_at?: string; updated_at?: string }): string {
  const start = new Date(task.started_at ?? task.created_at ?? "").getTime();
  if (!Number.isFinite(start)) return "—";
  const end = task.updated_at ? new Date(task.updated_at).getTime() : Date.now();
  return humanDuration(Math.max(0, (end - start) / 1000));
}

export function shortError(value: unknown): string {
  const text = String(value || "未知错误");
  return text.length > 180 ? `${text.slice(0, 177)}…` : text;
}
