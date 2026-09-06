<script setup lang="ts">
import AppIcon from "@/components/AppIcon.vue";
import StateBadge from "@/components/StateBadge.vue";
import { formatDate, mediaNames, statusNames } from "@/lib/format";
import { useAppStore, type AppView } from "@/stores/app";
import type { IconName } from "@/lib/icons";

const store = useAppStore();
const launchers: { view: AppView; icon: IconName; label: string }[] = [
  { view: "image", icon: "image", label: "图片生成" }, { view: "video", icon: "video", label: "视频生成" },
  { view: "speech", icon: "speech", label: "语音生成" }, { view: "music", icon: "music", label: "音乐合成" }
];
function taskTone(status: string) { return status === "succeeded" ? "ready" : ["failed", "interrupted"].includes(status) ? "error" : ["running", "assigned"].includes(status) ? "busy" : "neutral"; }
</script>

<template>
  <section class="workbench-view overview-view">
    <header class="overview-command">
      <div class="overview-presence"><i /><b>运行总览</b><small>{{ store.overview.services_online }} / {{ store.services.length }} 服务在线</small></div>
      <nav class="overview-launchbar" aria-label="生成工作台快捷入口">
        <button v-for="item in launchers" :key="item.view" type="button" @click="store.navigate(item.view)"><AppIcon :name="item.icon" :size="15" /><span>{{ item.label }}</span></button>
      </nav>
    </header>

    <div class="overview-console">
      <aside class="overview-services-pane">
        <header class="overview-pane-head"><div><small>SERVICES</small><h2>服务目录</h2></div><button type="button" class="link-button" @click="store.navigate('services')">配置</button></header>
        <div class="overview-service-list">
          <button v-for="service in store.services" :key="service.kind" type="button" class="service-summary" @click="store.navigate(service.kind)">
            <AppIcon :name="service.kind" :size="16" /><span><b>{{ service.name }}</b><small>{{ service.available ? `${service.models.filter(m => m.healthy).length} 个模型就绪 · ${service.active_tasks ?? 0} 个活动任务` : service.health_reason }}</small></span><StateBadge v-if="!service.available" label="离线" tone="error" /><AppIcon v-else class="service-enter" name="chevron" :size="13" />
          </button>
          <p v-if="!store.services.length" class="empty-state">尚未同步服务</p>
        </div>
      </aside>

      <main class="overview-activity-pane">
        <div class="overview-metrics" aria-label="运行指标">
          <article><small>在线服务</small><strong>{{ store.overview.services_online }}</strong><span>/ {{ store.services.length }}</span></article>
          <article><small>活动任务</small><strong>{{ store.overview.active_tasks }}</strong><span>实时</span></article>
          <article><small>已完成</small><strong>{{ store.overview.completed_tasks }}</strong><span>累计</span></article>
          <article :class="{ alert: store.overview.failed_tasks > 0 }"><small>异常</small><strong>{{ store.overview.failed_tasks }}</strong><span>累计</span></article>
        </div>
        <header class="overview-pane-head"><div><small>ACTIVITY</small><h2>最近任务</h2></div><button type="button" class="link-button" @click="store.toggleStatusCenter('task')">查看全部</button></header>
        <div class="task-list overview-task-list">
          <button v-for="task in store.tasks.slice(0, 12)" :key="task.id" type="button" class="task-line" @click="store.openTask(task.id)">
            <span class="task-kind"><AppIcon :name="task.service" :size="15" />{{ mediaNames[task.service] }}</span><b>{{ task.prompt }}</b><small>{{ task.model }}</small><StateBadge :label="statusNames[task.status] ?? task.status" :tone="taskTone(task.status)" /><time>{{ formatDate(task.updated_at) }}</time>
          </button>
          <p v-if="!store.tasks.length" class="empty-state">还没有任务，从上方工作台入口开始生成。</p>
        </div>
        <section class="overview-work-state" :class="{ live: store.overview.active_tasks > 0 }">
          <AppIcon :name="store.overview.active_tasks > 0 ? 'tasks' : 'check'" :size="24" />
          <b>{{ store.overview.active_tasks > 0 ? `${store.overview.active_tasks} 个任务正在执行` : '调度台待命' }}</b>
          <span>{{ store.overview.active_tasks > 0 ? '任务进度会通过实时事件持续更新' : '当前没有运行中的任务，可从顶部进入任一生成工作台' }}</span>
        </section>
      </main>
    </div>
  </section>
</template>
