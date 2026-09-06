<script setup lang="ts">
import { computed } from "vue";
import { useAppStore } from "@/stores/app";
import StatusCenter from "./StatusCenter.vue";

const store = useAppStore();
const gpuSummary = computed(() => {
  const configured = (store.gpuResources?.configured_gpu_indices ?? []) as number[];
  return configured.length ? `MediaCenter GPU ${configured.join(', ')}` : "未配置 GPU";
});
</script>

<template>
  <footer class="desktop-statusbar" @click.stop>
    <span class="connection-summary" :data-state="store.connectionState"><i /><b>{{ store.connectionLabel }}</b></span>
    <span>模型 {{ store.readyModelCount }} 就绪</span>
    <span>{{ gpuSummary }}</span>
    <span class="statusbar-spacer" />
    <StatusCenter kind="task" />
    <StatusCenter kind="message" />
  </footer>
</template>
