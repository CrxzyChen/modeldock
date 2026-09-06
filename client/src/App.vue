<script setup lang="ts">
import { onBeforeUnmount, onMounted } from "vue";
import DesktopTitleBar from "@/components/DesktopTitleBar.vue";
import SidebarNav from "@/components/SidebarNav.vue";
import StatusBar from "@/components/StatusBar.vue";
import ConfirmDialog from "@/components/ConfirmDialog.vue";
import AppIcon from "@/components/AppIcon.vue";
import LoginView from "@/views/LoginView.vue";
import OverviewView from "@/views/OverviewView.vue";
import ServicesView from "@/views/ServicesView.vue";
import HardwareView from "@/views/HardwareView.vue";
import AuditView from "@/views/AuditView.vue";
import GenerationWorkspace from "@/views/GenerationWorkspace.vue";
import VideoWorkspace from "@/views/VideoWorkspace.vue";
import ModelCenterView from "@/views/ModelCenterView.vue";
import ImageWorkspace from "@/views/ImageWorkspace.vue";
import { useAppStore } from "@/stores/app";

const store = useAppStore();

function onPointerDown(event: PointerEvent) {
  const target = event.target instanceof Element ? event.target : null;
  if (target?.closest(".status-center, .sidebar-account")) return;
  store.closeTransientUi();
}

function onKeyDown(event: KeyboardEvent) {
  if (event.key === "Escape") store.closeTransientUi();
}

function preventExternalDrop(event: DragEvent) { event.preventDefault(); }

onMounted(() => {
  document.addEventListener("pointerdown", onPointerDown);
  document.addEventListener("keydown", onKeyDown);
  document.addEventListener("dragover", preventExternalDrop);
  document.addEventListener("drop", preventExternalDrop);
  void store.initialize();
});
onBeforeUnmount(() => {
  document.removeEventListener("pointerdown", onPointerDown);
  document.removeEventListener("keydown", onKeyDown);
  document.removeEventListener("dragover", preventExternalDrop);
  document.removeEventListener("drop", preventExternalDrop);
});
</script>

<template>
  <div v-if="!store.initialized" class="loading-screen" aria-label="正在启动"><i /></div>
  <LoginView v-else-if="!store.authenticated" />
  <div v-else class="desktop-shell">
    <DesktopTitleBar />
    <SidebarNav />
    <main class="workbench">
      <OverviewView v-show="store.activeView === 'overview'" />
      <ServicesView v-show="store.activeView === 'services'" />
      <HardwareView v-show="store.activeView === 'hardware'" />
      <AuditView v-show="store.activeView === 'audit'" />
      <GenerationWorkspace v-show="store.activeView === 'speech'" kind="speech" />
      <GenerationWorkspace v-show="store.activeView === 'music'" kind="music" />
      <VideoWorkspace v-show="store.activeView === 'video'" />
      <ModelCenterView v-show="store.activeView === 'deployments'" />
      <ImageWorkspace v-show="store.activeView === 'image'" />
    </main>
    <StatusBar />
  </div>
  <ConfirmDialog />
  <aside class="toast-stack" aria-live="polite">
    <article v-for="item in store.toasts" :key="item.id" class="toast-item" :class="item.type">
      <div><b>{{ item.title }}</b><small v-if="item.detail">{{ item.detail }}</small></div>
      <button type="button" aria-label="关闭提示" @click="store.dismissToast(item.id)"><AppIcon name="close" :size="14" /></button>
    </article>
  </aside>
</template>
