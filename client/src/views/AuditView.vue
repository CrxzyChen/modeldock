<script setup lang="ts">
import ActionButton from "@/components/ActionButton.vue";
import PageHeader from "@/components/PageHeader.vue";
import { formatDate } from "@/lib/format";
import { useAppStore } from "@/stores/app";
const store = useAppStore();
</script>

<template>
  <section class="workbench-view scroll-view page-view audit-view">
    <PageHeader kicker="AUDIT TRAIL" title="审计日志" description="服务器记录的任务、安装、实例与配置状态变化。">
      <ActionButton action-key="audit:refresh" icon="refresh" label="刷新" compact @click="store.refreshAudit" />
    </PageHeader>
    <section class="surface audit-timeline"><article v-for="event in store.audit" :key="event.id"><time>{{ formatDate(event.occurred_at) }}</time><i /><div><b>{{ event.action }} · {{ event.target }}</b><p>{{ JSON.stringify(event.detail) }}</p></div></article><p v-if="!store.audit.length" class="empty-state">暂无审计事件。</p></section>
  </section>
</template>
