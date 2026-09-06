<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import ActionButton from "./ActionButton.vue";
import StateBadge from "./StateBadge.vue";
import { canCancelTask, formatDate, taskElapsed, taskErrorMessage, taskRetryDisabledReason, taskStageLabel, taskStatusLabel } from "@/lib/format";
import { useAppStore } from "@/stores/app";
import type { MediaKind, MediaTask } from "@/types/contracts";

const props = defineProps<{ task: MediaTask | null; kind: MediaKind }>();
const emit = defineEmits<{ reuse: [task: MediaTask] }>();
const store = useAppStore();
const mediaUrl = ref("");
const loadedPath = ref("");
const loading = ref(false);
const loadError = ref("");
const terminal = computed(() => props.task && ["succeeded", "failed", "canceled", "interrupted"].includes(props.task.status));
const inFlight = computed(() => props.task && ["queued", "assigned", "running", "cancel_requested"].includes(props.task.status));
const tone = computed(() => props.task?.status === "succeeded" ? "ready" : ["failed", "interrupted"].includes(props.task?.status ?? "") ? "error" : props.task?.status === "running" ? "busy" : "neutral");
const artifact = computed(() => props.task?.output?.artifact_url ?? "");

async function loadArtifact() {
  const path = artifact.value;
  if (!path || path === loadedPath.value || loading.value) return;
  loading.value = true; loadError.value = "";
  const url = await store.runAction(`artifact:preview:${props.task?.id}`, "正在读取产物", () => store.fetchArtifactUrl(path), { record: false });
  if (url) { if (mediaUrl.value) URL.revokeObjectURL(mediaUrl.value); mediaUrl.value = url; loadedPath.value = path; }
  else loadError.value = "产物预览失败，可尝试导出到本地。";
  loading.value = false;
}
watch(artifact, path => { if (!path) return; void loadArtifact(); }, { immediate: true });
onBeforeUnmount(() => { if (mediaUrl.value) URL.revokeObjectURL(mediaUrl.value); });
</script>

<template>
  <section class="task-monitor surface">
    <header class="surface-head"><div><small>CURRENT OUTPUT</small><h2>当前任务</h2></div><button type="button" class="link-button" @click="store.toggleStatusCenter('task')">后台任务</button></header>
    <div v-if="task" class="task-monitor-body">
      <header><div><code>{{ task.id }}</code><b>{{ task.model }}</b></div><StateBadge :label="taskStatusLabel(task)" :tone="tone" /></header>
      <p>{{ task.prompt }}</p>
      <div v-if="inFlight" class="task-progress"><span><b>{{ taskStageLabel(task) }}</b><small>{{ taskElapsed(task) }}</small></span><i><em :style="{ width: `${Math.round(Number(task.progress ?? 0) * 100)}%` }" /></i><small>{{ Math.round(Number(task.progress ?? 0) * 100) }}%</small></div>
      <div v-if="['failed', 'interrupted'].includes(task.status)" class="task-error"><b>{{ taskStatusLabel(task) }}</b><p>{{ taskErrorMessage(task) }}</p></div>
      <div v-if="artifact" class="media-preview">
        <div v-if="loading" class="preview-loading"><i />正在读取产物</div>
        <img v-else-if="kind === 'image' && mediaUrl" :src="mediaUrl" alt="生成图片">
        <video v-else-if="kind === 'video' && mediaUrl" :src="mediaUrl" controls preload="metadata" />
        <audio v-else-if="mediaUrl" :src="mediaUrl" controls preload="metadata" />
        <p v-if="loadError">{{ loadError }}</p>
      </div>
      <footer><span>{{ taskStageLabel(task) }} · {{ formatDate(task.updated_at) }}</span><div><ActionButton v-if="inFlight" :action-key="`task:${task.id}:cancel`" icon="stop" :label="canCancelTask(task) ? '取消' : '正在停止'" :disabled="!canCancelTask(task)" :title="canCancelTask(task) ? '取消任务' : '已请求停止，等待执行退出确认'" tone="danger" compact @click="store.cancelTask(task)" /><ActionButton v-if="terminal && task.status !== 'succeeded'" :action-key="`task:${task.id}:retry`" icon="refresh" label="重试" :disabled="Boolean(taskRetryDisabledReason(task))" :title="taskRetryDisabledReason(task) || '按原任务配置重试'" compact @click="store.retryTask(task)" /><ActionButton v-if="terminal" icon="redo" label="复用参数" compact @click="emit('reuse', task)" /><ActionButton v-if="artifact" :action-key="`artifact:save:${artifact}`" icon="export" label="导出" compact @click="store.saveArtifact(artifact)" /></div></footer>
    </div>
    <div v-else class="task-monitor-empty"><span>当前工作台还没有任务</span><small>提交后在这里持续显示排队、运行阶段与产物。</small></div>
  </section>
</template>
