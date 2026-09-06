<script setup lang="ts">
import { computed } from "vue";
import AppIcon from "./AppIcon.vue";
import { desktopBridge } from "@/services/desktop";
import { useAppStore, type AppView } from "@/stores/app";

const store = useAppStore();
const labels: Record<AppView, string> = {
  overview: "运行总览", image: "图片生成", video: "视频生成", speech: "语音生成",
  music: "音乐合成", services: "服务配置", deployments: "模型管理",
  hardware: "服务器硬件", audit: "审计日志",
};
const workspaceLabel = computed(() => labels[store.activeView]);

function control(action: string) {
  void desktopBridge().windowControl(action);
}
</script>

<template>
  <header class="desktop-titlebar">
    <span class="desktop-title"><i aria-hidden="true">MC</i><b>MediaCenter</b></span>
    <span class="desktop-workspace-label">{{ workspaceLabel }}</span>
    <div class="window-actions">
      <button type="button" aria-label="最小化" title="最小化" @click="control('minimize')"><AppIcon name="minimize" :size="14" /></button>
      <button type="button" aria-label="最大化或还原" title="最大化或还原" @click="control('toggle-maximize')"><AppIcon name="maximize" :size="13" /></button>
      <button type="button" class="window-close" aria-label="关闭" title="关闭" @click="control('close')"><AppIcon name="close" :size="14" /></button>
    </div>
  </header>
</template>
