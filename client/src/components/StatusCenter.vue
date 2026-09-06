<script setup lang="ts">
import { computed } from "vue";
import AppIcon from "./AppIcon.vue";
import ActionButton from "./ActionButton.vue";
import { deploymentOperationLabels } from "@/lib/models";
import { formatDate, humanBytes, mediaNames, taskStatusLabel, taskStageLabel } from "@/lib/format";
import { useAppStore, type StatusCenterKind } from "@/stores/app";

const props = defineProps<{ kind: StatusCenterKind }>();
const store = useAppStore();
const open = computed(() => store.openStatusCenter === props.kind);
const badge = computed(() => props.kind === "task" ? store.backgroundCount : store.unreadCount);
const installationTransferIds = computed(() => new Set(store.installations.map(item => item.transfer_id).filter(Boolean)));
const visibleTransfers = computed(() => store.transfers.filter(item => !installationTransferIds.value.has(item.id)).slice(0, 12));
const terminalDeploymentStates = new Set(["ready", "failed", "canceled"]);
const visibleDeployments = computed(() => {
  const recent = [...store.deploymentOperations].sort((left, right) =>
    String(right.updated_at ?? right.created_at ?? "").localeCompare(String(left.updated_at ?? left.created_at ?? "")));
  return [...recent.filter(item => !terminalDeploymentStates.has(item.state)),
    ...recent.filter(item => terminalDeploymentStates.has(item.state)).slice(0, 12)];
});

function openMessage(id: string) { store.markMessageRead(id); }
function transferActions(item: Record<string, any>): Array<"pause" | "resume" | "cancel" | "retry"> {
  if (item.state === "paused") return ["resume", "cancel"];
  if (["queued", "transferring"].includes(item.state)) return ["pause", "cancel"];
  if (item.state === "failed" && item.direction === "download") return ["retry"];
  return [];
}
const transferLabels = { pause: "暂停", resume: "继续", cancel: "取消", retry: "重试" };
</script>

<template>
  <div class="status-center" @click.stop>
    <button type="button" class="status-center-trigger" :title="kind === 'task' ? '后台任务' : '消息'" :aria-label="kind === 'task' ? '后台任务' : '消息'" :aria-expanded="open" @click="store.toggleStatusCenter(kind)">
      <AppIcon :name="kind === 'task' ? 'tasks' : 'messages'" :size="16" />
      <b v-if="badge" class="status-center-badge">{{ badge > 99 ? '99+' : badge }}</b>
    </button>
    <Transition name="popover">
      <section v-if="open" class="status-center-panel">
        <header>
          <div><b>{{ kind === 'task' ? '后台任务' : '消息' }}</b><small>{{ store.activeProfile?.name ?? '当前服务器' }}</small></div>
          <span v-if="kind === 'message'">
            <button type="button" @click="store.markAllMessagesRead">全部已读</button>
            <button type="button" @click="store.clearMessages">清空</button>
          </span>
        </header>
        <div v-if="kind === 'task'" class="status-center-list">
          <article v-for="item in visibleDeployments" :key="`deployment:${item.id}`" class="status-row static deployment-status-row">
            <span>{{ item.payload?.action === 'uninstall' ? '服务卸载' : item.payload?.expected_configuration ? '实例配置' : '模型部署' }} · {{ item.deployment_id }}</span>
            <b>{{ item.payload?.action === 'uninstall' ? (item.state === 'ready' ? '已卸载 · 资产保留' : item.state === 'failed' ? '卸载未完成' : '正在移除容器') : deploymentOperationLabels[item.state] ?? item.state }}</b>
            <small>{{ item.id }} · {{ formatDate(item.updated_at ?? item.created_at) }}</small>
            <small v-if="item.error_message || item.error_code">{{ item.error_message || item.error_code }}</small>
            <nav class="status-row-actions">
              <ActionButton v-if="item.payload?.action !== 'uninstall' && !terminalDeploymentStates.has(item.state) && !['canceling', 'rollback'].includes(item.state)"
                :action-key="`deployment-operation:${item.id}:cancel`" icon="stop" label="取消" compact
                @click="store.cancelDeploymentOperation(item)" />
              <ActionButton v-if="item.payload?.action === 'uninstall' && item.state === 'failed'"
                :action-key="`uninstall:deployment:${item.deployment_id}`" icon="refresh" label="重试卸载" compact
                :disabled="!store.deployments.some(value => value.id === item.deployment_id && value.removal_operation_id === item.id)"
                @click="store.retryUserRemoval(item)" />
              <ActionButton v-else-if="item.payload?.expected_configuration && ['failed', 'canceled'].includes(item.state)"
                icon="settings" label="重新配置" compact @click="store.openModelSettings(item.deployment_id)" />
              <ActionButton v-else-if="item.payload && item.payload.action !== 'uninstall' && (item.state === 'canceled' || (item.state === 'failed' && item.error_class === 'recoverable'))"
                :action-key="`deployment:create:${item.deployment_id}`" icon="refresh" label="重试" compact
                @click="store.createUserDeployment(item.payload)" />
            </nav>
          </article>
          <button v-for="task in store.tasks.slice(0, 12)" :key="`task:${task.id}`" type="button" class="status-row" @click="store.openTask(task.id)">
            <span>{{ mediaNames[task.service] }} · {{ task.model }}</span><b>{{ task.prompt }}</b>
            <small>{{ taskStatusLabel(task) }} · {{ taskStageLabel(task) }} · {{ formatDate(task.updated_at) }}</small>
            <i v-if="task.status === 'running'" class="row-progress"><em :style="{ width: `${Math.round((task.progress ?? 0) * 100)}%` }" /></i>
          </button>
          <div v-for="item in store.installations.slice(0, 12)" :key="`install:${item.id}`" class="status-row static">
            <span>服务安装 · {{ item.recipe_key }}</span><b>{{ item.current_step ?? item.state }}</b><small>{{ Math.round(Number(item.progress ?? 0) * 100) }}% · {{ formatDate(item.updated_at) }}</small>
            <i class="row-progress"><em :style="{ width: `${Math.round(Number(item.progress ?? 0) * 100)}%` }" /></i>
          </div>
          <div v-for="item in visibleTransfers" :key="`transfer:${item.id}`" class="status-row static">
            <span>模型传输 · {{ item.direction }}</span><b>{{ item.display_name }}</b><small>{{ item.state }} · {{ humanBytes(item.received_bytes) }} / {{ humanBytes(item.expected_bytes) }}</small>
            <nav v-if="transferActions(item).length" class="status-row-actions"><button v-for="action in transferActions(item)" :key="action" type="button" @click="store.controlTransfer(item, action)">{{ transferLabels[action] }}</button></nav>
          </div>
          <p v-if="!store.tasks.length && !store.installations.length && !visibleTransfers.length && !visibleDeployments.length" class="status-empty">当前服务器还没有任务</p>
        </div>
        <div v-else class="status-center-list">
          <article v-for="message in store.statusMessages" :key="message.id" class="message-row" :class="{ unread: !message.read, expanded: store.expandedMessageId === message.id }">
            <button type="button" class="message-main" @click="openMessage(message.id)">
              <span>{{ message.serverName }} · {{ formatDate(message.createdAt) }}</span><b><AppIcon v-if="message.type === 'error'" name="warning" :size="12" />{{ message.title }}</b>
              <small>{{ message.detail || (message.taskId ? `任务 ${message.taskId}` : '客户端消息') }}</small>
            </button>
            <div v-if="store.expandedMessageId === message.id" class="message-detail">
              <p>{{ message.detail || '没有更多详情。' }}</p>
              <button v-if="message.taskId && message.serverId === store.activeProfile?.id" type="button" @click="store.openTask(message.taskId)">查看任务</button>
            </div>
          </article>
          <p v-if="!store.statusMessages.length" class="status-empty">暂时没有消息</p>
        </div>
      </section>
    </Transition>
  </div>
</template>
