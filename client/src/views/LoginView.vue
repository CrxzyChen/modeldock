<script setup lang="ts">
import { computed, reactive, watch } from "vue";
import ActionButton from "@/components/ActionButton.vue";
import { useAppStore } from "@/stores/app";

const store = useAppStore();
const form = reactive({ profileId: "", name: "", baseUrl: "", apiKey: "", allowInsecureSourceAuthorization: false });
const selected = computed(() => store.connections.profiles.find(item => item.id === form.profileId));

function populate(profileId?: string | null) {
  const profile = store.connections.profiles.find(item => item.id === profileId);
  form.profileId = profile?.id ?? ""; form.name = profile?.name ?? ""; form.baseUrl = profile?.baseUrl ?? "";
  form.apiKey = ""; form.allowInsecureSourceAuthorization = profile?.allowInsecureSourceAuthorization === true;
}
watch(() => store.loginSelection, populate, { immediate: true });
watch(() => form.profileId, value => populate(value));

async function submit() {
  await store.connectServer({ profileId: form.profileId || undefined, name: form.name, baseUrl: form.baseUrl, apiKey: form.apiKey, allowInsecureSourceAuthorization: form.allowInsecureSourceAuthorization });
  form.apiKey = "";
}
</script>

<template>
  <main class="login-screen">
    <form class="login-panel" @submit.prevent="submit">
      <header><small>MEDIACENTER DESKTOP</small><h1>连接模型服务中心</h1><p>选择已保存服务器，或添加新的 MediaCenter Server。</p></header>
      <label>服务器档案
        <select v-model="form.profileId"><option value="">添加新服务器</option><option v-for="profile in store.connections.profiles" :key="profile.id" :value="profile.id">{{ profile.name }} · {{ profile.baseUrl }}</option></select>
      </label>
      <label>名称<input v-model.trim="form.name" required maxlength="60" placeholder="MediaCenter Server"></label>
      <label>服务器地址<input v-model.trim="form.baseUrl" required inputmode="url" placeholder="http://10.0.0.10:8787"></label>
      <label>API Key<input v-model.trim="form.apiKey" :required="!selected?.hasCredential" type="password" autocomplete="off" placeholder="输入服务器 API Key"></label>
      <label v-if="form.baseUrl.startsWith('http://') && !/^http:\/\/(localhost|127\.)/i.test(form.baseUrl)" class="trust-option">
        <input v-model="form.allowInsecureSourceAuthorization" type="checkbox"><span>我信任此局域网，允许向该服务器提交专用模型来源令牌</span>
      </label>
      <p v-if="store.loginMessage" class="form-message">{{ store.loginMessage }}</p>
      <div class="login-actions">
        <button v-if="selected" type="button" class="text-danger" @click="store.removeServer(selected.id)">移除档案</button>
        <ActionButton action-key="connect" icon="play" label="连接" tone="primary" @click="submit" />
      </div>
      <small class="login-footnote">连接档案保存在本机；API Key 由操作系统加密保存。</small>
    </form>
  </main>
</template>
