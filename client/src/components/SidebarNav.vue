<script setup lang="ts">
import { computed } from "vue";
import AppIcon from "./AppIcon.vue";
import { useAppStore, type AppView } from "@/stores/app";
import type { IconName } from "@/lib/icons";

const store = useAppStore();
const sections: { view: AppView; label: string; icon: IconName }[] = [
  { view: "overview", label: "运行总览", icon: "overview" },
  { view: "image", label: "图片生成", icon: "image" },
  { view: "video", label: "视频生成", icon: "video" },
  { view: "speech", label: "语音生成", icon: "speech" },
  { view: "music", label: "音乐合成", icon: "music" },
  { view: "services", label: "服务配置", icon: "services" },
  { view: "deployments", label: "模型管理", icon: "models" },
  { view: "hardware", label: "服务器硬件", icon: "hardware" },
  { view: "audit", label: "审计日志", icon: "audit" }
];
const profileName = computed(() => store.activeProfile?.name ?? "未连接");
</script>

<template>
  <aside class="sidebar-nav" @click.stop>
    <nav aria-label="主导航">
      <button
        v-for="item in sections" :key="item.view" type="button" class="sidebar-item"
        :class="{ active: store.activeView === item.view }" :title="item.label" :aria-label="item.label"
        @click="store.navigate(item.view)"
      ><AppIcon :name="item.icon" /></button>
    </nav>
    <div class="sidebar-account">
      <button type="button" class="sidebar-item" :class="{ active: store.userMenuOpen }" title="账户与服务器" aria-label="账户与服务器" :aria-expanded="store.userMenuOpen" @click="store.userMenuOpen = !store.userMenuOpen">
        <AppIcon name="user" />
      </button>
      <section v-if="store.userMenuOpen" class="account-menu">
        <header><b>{{ profileName }}</b><small>{{ store.activeProfile?.baseUrl }}</small></header>
        <button type="button" @click="store.authenticated = false; store.loginMessage = ''; store.userMenuOpen = false">切换服务器</button>
        <button type="button" class="danger" @click="store.logout">注销当前服务器</button>
      </section>
    </div>
  </aside>
</template>
