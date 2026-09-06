<script setup lang="ts">
import { computed } from "vue";
import ActionButton from "@/components/ActionButton.vue";
import PageHeader from "@/components/PageHeader.vue";
import RadialGauge from "@/components/RadialGauge.vue";
import StateBadge from "@/components/StateBadge.vue";
import { formatDate, humanBytes, humanDuration, mediaNames } from "@/lib/format";
import type { MediaKind } from "@/types/contracts";
import { useAppStore } from "@/stores/app";

const store = useAppStore();
const data = computed(() => store.hardware ?? {});
function percent(value: unknown, total: unknown = 100) { const n = Number(value), d = Number(total); return Number.isFinite(n) && Number.isFinite(d) && d > 0 ? Math.round(n / d * 100) : null; }
function mediaLabel(kind: string) { return mediaNames[kind as MediaKind] ?? kind; }
</script>

<template>
  <section class="workbench-view scroll-view page-view hardware-view">
    <PageHeader kicker="SERVER OBSERVABILITY" title="服务器硬件" description="中立展示操作系统与驱动观测事实；进程名称不代表 MediaCenter 推断了服务归属。">
      <ActionButton action-key="hardware:refresh" icon="refresh" label="刷新" compact @click="store.refreshHardware" />
    </PageHeader>
    <div v-if="store.hardware" class="hardware-content">
      <div class="hardware-facts">
        <article class="surface"><small>HOST / OS</small><strong>{{ data.host?.hostname ?? '不可用' }}</strong><span>{{ [data.host?.operating_system, data.host?.release, data.host?.architecture].filter(Boolean).join(' · ') }}</span><em>运行 {{ humanDuration(data.host?.uptime_seconds) }}</em></article>
        <article class="surface"><small>CPU</small><strong>{{ data.cpu?.model ?? '不可用' }}</strong><span>{{ data.cpu?.sockets ?? '—' }} 路 · {{ data.cpu?.physical_cores ?? '—' }} 核 · {{ data.cpu?.logical_processors ?? '—' }} 线程</span></article>
        <article class="surface memory-fact"><div><small>MEMORY</small><strong>{{ humanBytes(Number(data.memory?.total_mib) * 1048576) }}</strong><span>已用 {{ humanBytes(Number(data.memory?.used_mib) * 1048576) }}</span></div><RadialGauge label="内存" :value="percent(data.memory?.used_mib, data.memory?.total_mib)" /></article>
      </div>
      <section class="hardware-section"><header><div><small>PHYSICAL GPU INVENTORY</small><h2>GPU 与计算进程</h2></div><span>来自 nvidia-smi</span></header>
        <div class="gpu-grid"><article v-for="gpu in data.gpu?.items ?? []" :key="gpu.uuid || gpu.index" class="surface gpu-card"><header><div><small>GPU {{ gpu.index }}</small><h3>{{ gpu.name }}</h3></div><StateBadge :label="`${Math.max(0, (gpu.memory_total_mib ?? 0) - (gpu.memory_used_mib ?? 0))} MiB 空闲`" tone="neutral" /></header><div class="gauge-pair"><RadialGauge label="利用率" :value="percent(gpu.utilization_percent)" :detail="`${gpu.utilization_percent ?? '—'}%`" /><RadialGauge label="显存" :value="percent(gpu.memory_used_mib, gpu.memory_total_mib)" :detail="`${gpu.memory_used_mib ?? '—'} / ${gpu.memory_total_mib ?? '—'} MiB`" /></div><dl><div><dt>温度</dt><dd>{{ gpu.temperature_celsius ?? '—' }} °C</dd></div><div><dt>功耗</dt><dd>{{ gpu.power_draw_watts ?? '—' }} / {{ gpu.power_limit_watts ?? '—' }} W</dd></div><div><dt>驱动</dt><dd>{{ gpu.driver_version ?? '—' }}</dd></div></dl><div class="process-list"><small>可观测计算进程</small><code v-for="process in gpu.processes ?? []" :key="process.pid">PID {{ process.pid }} · {{ process.process_name }} · {{ process.memory_mib }} MiB</code><span v-if="!gpu.processes?.length">未观测到计算进程</span></div></article><p v-if="!data.gpu?.items?.length" class="empty-state surface">GPU 遥测不可用：{{ data.gpu?.error }}</p></div>
      </section>
      <section class="hardware-section"><header><div><small>FILESYSTEM CAPACITY</small><h2>存储容量</h2></div></header><div class="storage-grid"><article v-for="disk in data.storage?.items ?? []" :key="disk.path" class="surface storage-card"><header><span>{{ disk.path }}</span><b>{{ percent(disk.used_bytes, disk.total_bytes) }}%</b></header><strong>{{ humanBytes(disk.total_bytes) }}</strong><i><em :style="{ width: `${percent(disk.used_bytes, disk.total_bytes) ?? 0}%` }" /></i><small>已用 {{ humanBytes(disk.used_bytes) }} · 可用 {{ humanBytes(disk.free_bytes) }}</small></article></div></section>
      <section class="surface process-summary"><header><div><small>MEDIACENTER PROCESS</small><h2>服务状态</h2></div><span>采集于 {{ formatDate(data.captured_at) }}</span></header><p>PID {{ data.mediacenter?.pid ?? '—' }} · 运行 {{ humanDuration(data.mediacenter?.process_uptime_seconds) }} · 配置 GPU {{ data.mediacenter?.configured_gpu_indices?.join(', ') || '无' }}</p><div><span v-for="service in data.mediacenter?.services ?? []" :key="service.kind"><b>{{ mediaLabel(service.kind) }}</b><small>{{ service.healthy_models }}/{{ service.total_models }} 模型就绪</small><StateBadge :label="service.status === 'online' ? '在线' : '离线'" :tone="service.status === 'online' ? 'ready' : 'error'" /></span></div></section>
    </div>
    <p v-else class="empty-state surface">正在读取服务器硬件状态…</p>
  </section>
</template>
