<script setup lang="ts">
import { reactive, watchEffect } from "vue";
import ActionButton from "@/components/ActionButton.vue";
import AppIcon from "@/components/AppIcon.vue";
import PageHeader from "@/components/PageHeader.vue";
import StateBadge from "@/components/StateBadge.vue";
import { useAppStore } from "@/stores/app";
import type { MediaKind, MediaService } from "@/types/contracts";

const store = useAppStore();
const drafts = reactive<Record<string, { enabled: boolean; timeout: number }>>({});
watchEffect(() => store.services.forEach(item => {
  if (!drafts[item.kind]) drafts[item.kind] = { enabled: item.enabled, timeout: item.timeout_seconds ?? 600 };
}));
function save(service: MediaService) { const value = drafts[service.kind]; return store.configureService(service, { enabled: value.enabled, timeout_seconds: value.timeout }); }
function toggle(service: MediaService) { drafts[service.kind].enabled = !drafts[service.kind].enabled; return save(service); }
function icon(kind: MediaKind) { return kind; }
</script>

<template>
  <section class="workbench-view page-view services-view">
    <PageHeader kicker="SERVICE CONFIGURATION" title="服务配置" description="启停业务入口并设置任务超时；模型实例的运行状态在模型管理中控制。">
      <ActionButton action-key="snapshot" icon="refresh" title="刷新" compact @click="store.refreshSnapshot(true)" />
    </PageHeader>
    <section class="service-config-list" aria-label="媒体服务配置">
      <header class="service-list-columns"><span>服务与运行模型</span><span>任务超时</span><span>服务状态</span><span>操作</span></header>
      <article v-for="service in store.services" :key="service.kind" class="service-config-row">
        <div class="service-identity"><span class="service-icon"><AppIcon :name="icon(service.kind)" :size="18" /></span><div><h2>{{ service.name }}</h2><p>{{ service.description }}</p><section class="service-model-summary"><span v-for="model in service.models" :key="model.model_key"><i :class="{ ready: model.healthy }" />{{ model.label }}</span><small v-if="!service.models.length">{{ service.health_reason || '没有模型实例' }}</small></section></div></div>
        <label class="service-timeout"><input v-model.number="drafts[service.kind].timeout" type="number" min="10" max="7200"><span>秒</span></label>
        <StateBadge :label="service.available ? '可用' : '离线'" :tone="service.available ? 'ready' : 'error'" />
        <div class="inline-actions"><button type="button" class="switch-control" :class="{ on: drafts[service.kind].enabled }" :aria-label="drafts[service.kind].enabled ? '停用服务' : '启用服务'" @click="toggle(service)"><i /></button><ActionButton :action-key="`service:${service.kind}:save`" icon="save" title="保存服务设置" compact @click="save(service)" /></div>
      </article>
      <p v-if="!store.services.length" class="empty-state">服务数据尚未同步。</p>
      <footer v-else class="service-work-state">
        <AppIcon name="services" :size="24" />
        <b>{{ store.services.filter(item => item.enabled).length }} 个业务入口已启用</b>
        <span>容器启动和模型显存策略在模型管理中维护</span>
      </footer>
    </section>
  </section>
</template>
