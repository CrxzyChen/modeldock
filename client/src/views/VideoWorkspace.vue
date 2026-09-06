<script setup lang="ts">
import { computed, reactive, ref, watch } from "vue";
import ActionButton from "@/components/ActionButton.vue";
import AppIcon from "@/components/AppIcon.vue";
import OptionFields from "@/components/OptionFields.vue";
import StateBadge from "@/components/StateBadge.vue";
import TaskMonitor from "@/components/TaskMonitor.vue";
import { api } from "@/services/api";
import { useAppStore } from "@/stores/app";
import type { MediaAsset, MediaTask, ServiceModel } from "@/types/contracts";

type Draft = { prompt: string; options: Record<string, string | number> };
const store = useAppStore();
const selectedModelKey = ref("");
const prompt = ref("");
const options = reactive<Record<string, string | number>>({});
const drafts = reactive<Record<string, Draft>>({});
const dropActive = ref(false);
const service = computed(() => store.services.find(item => item.kind === "video"));
const models = computed(() => service.value?.models ?? []);
const model = computed(() => models.value.find(item => item.model_key === selectedModelKey.value) ?? null);
const capability = computed<Record<string, any>>(() => model.value?.capabilities ?? { options: [] });
const contract = computed(() => capability.value.input_contract ?? { minimum: 0, maximum: 0, accept: [] });
const assets = computed(() => store.generationInputs[store.generationInputKey("video", selectedModelKey.value)] ?? []);
const basicFields = computed(() => (capability.value.options ?? []).filter((field: any) => !field.advanced));
const advancedFields = computed(() => (capability.value.options ?? []).filter((field: any) => field.advanced));
const task = computed(() => store.selectedTask("video"));
const workflow = computed(() => contract.value.maximum > 1 ? "ref2va" : contract.value.maximum === 1 && contract.value.accept.some((type: string) => type.startsWith("image/")) ? "i2v" : "t2v");
const workflowName = computed(() => ({ t2v: "文生视频", i2v: "首帧动画", ref2va: "多参考驱动" })[workflow.value]);
const inputValid = computed(() => assets.value.length >= contract.value.minimum && assets.value.length <= contract.value.maximum);
const ready = computed(() => Boolean(service.value?.enabled && model.value?.healthy && inputValid.value));
const duration = computed(() => Number(options.num_frames) && Number(options.fps) ? `${(Number(options.num_frames) / Number(options.fps)).toFixed(1)} 秒` : "由模型决定");
const size = computed(() => options.width && options.height ? `${options.width} × ${options.height}` : "模型默认");

function chooseDefault() {
  if (models.value.some(item => item.model_key === selectedModelKey.value)) return;
  selectedModelKey.value = models.value.find(item => item.model_key === service.value?.default_model && item.healthy)?.model_key
    ?? models.value.find(item => item.healthy)?.model_key ?? models.value[0]?.model_key ?? "";
}
function saveDraft(key: string) { if (key) drafts[key] = { prompt: prompt.value, options: { ...options } }; }
function loadModel(value: ServiceModel | null, previous?: ServiceModel | null) {
  if (previous) saveDraft(previous.model_key);
  Object.keys(options).forEach(key => delete options[key]);
  const draft = value ? drafts[value.model_key] : undefined;
  prompt.value = draft?.prompt ?? "";
  (value?.capabilities?.options ?? []).forEach((field: any) => { options[field.key] = draft?.options[field.key] ?? field.default; });
}
watch(models, chooseDefault, { immediate: true });
watch(model, loadModel, { immediate: true });
async function pickAssets() { if (model.value) await store.uploadPickedAssets("video", contract.value.maximum > 1, contract.value.maximum, model.value.model_key, contract.value.accept); }
async function dropAssets(event: DragEvent) {
  dropActive.value = false;
  if (!model.value || !event.dataTransfer?.files.length) return;
  await store.uploadDroppedAssets("video", [...event.dataTransfer.files], contract.value.maximum, model.value.model_key, contract.value.accept);
}
function moveAsset(index: number, delta: number) { const next = [...assets.value]; const to = index + delta; if (to < 0 || to >= next.length) return; [next[index], next[to]] = [next[to], next[index]]; store.generationInputs[store.generationInputKey("video", selectedModelKey.value)] = next; }
function typeOf(asset: MediaAsset) { const type = String(asset.media_type ?? asset.content_type ?? ""); return type.startsWith("image/") ? "Picture" : type.startsWith("video/") ? "Video" : "Audio"; }
function referenceOf(index: number) { const row = assets.value[index]; const type = typeOf(row); return `${type} ${assets.value.slice(0, index + 1).filter(item => typeOf(item) === type).length}`; }
function insertReference() { if (!assets.value.length) return; prompt.value += `${prompt.value && !prompt.value.endsWith(" ") ? " " : ""}<${referenceOf(0)}>`; }
function useTemplate() { prompt.value = capability.value.workflow_guide?.prompt_template ?? prompt.value; }
async function submit() {
  if (!model.value || !prompt.value.trim() || !inputValid.value) return;
  saveDraft(model.value.model_key);
  const result = await store.submitTask("video", model.value.model_key, prompt.value.trim(), { ...options }, assets.value.map(item => item.id));
  if (result) { prompt.value = ""; store.generationInputs[store.generationInputKey("video", model.value.model_key)] = []; drafts[model.value.model_key] = { prompt: "", options: { ...options } }; }
}
async function reuse(task: MediaTask) {
  const target = models.value.find(item => item.model_key === task.model);
  if (!target) { store.toast("原任务使用的模型当前不可用", { type: "error", persistent: true }); return; }
  selectedModelKey.value = target.model_key; await Promise.resolve();
  prompt.value = task.prompt; Object.entries(task.options ?? {}).forEach(([key, value]) => { options[key] = value as string | number; });
  const loaded = await Promise.all((task.inputs ?? []).map(id => api<MediaAsset>(`/api/v1/assets/${encodeURIComponent(id)}`)));
  store.generationInputs[store.generationInputKey("video", target.model_key)] = loaded;
  saveDraft(target.model_key); store.toast("模型、提示词、参数和素材已回填", { record: false });
}
</script>

<template>
  <section class="workbench-view video-workspace">
    <form class="video-form" @submit.prevent="submit">
      <header class="workspace-header"><div><small>VIDEO WORKSPACE</small><h1>视频生成</h1></div><StateBadge :label="ready ? '可提交' : model?.healthy ? '等待输入' : '模型离线'" :tone="ready ? 'ready' : model?.healthy ? 'warn' : 'error'" /></header>
      <section class="video-scroll">
        <div class="video-block surface"><header><div><small>MODEL</small><h2>运行模型</h2></div><span class="workflow-badge">{{ workflowName }}</span></header><label class="field-block"><select v-model="selectedModelKey"><option v-for="item in models" :key="item.model_key" :value="item.model_key" :disabled="!item.healthy">{{ item.label }}{{ item.healthy ? '' : ' · 离线' }}</option></select></label><p class="model-contract">{{ model?.model_id ?? model?.model_key }} · 修订 {{ model?.revision ?? '—' }} · {{ model?.gpu_indices?.length ? `GPU ${model.gpu_indices.join(' + ')}` : '未分配 GPU' }}</p></div>
        <div class="video-block surface" :class="{ 'drop-active': dropActive }" @dragenter.prevent="dropActive = true" @dragover.prevent @dragleave.self="dropActive = false" @drop.prevent="dropAssets"><header><div><small>SOURCE</small><h2>{{ workflow === 'i2v' ? '起始画面' : workflow === 'ref2va' ? '参考素材轨道' : '文本驱动' }}</h2></div><span>{{ contract.minimum }}–{{ contract.maximum }} 个素材</span></header><div v-if="contract.maximum" class="reference-list"><article v-for="(asset,index) in assets" :key="asset.id"><span><AppIcon :name="typeOf(asset) === 'Picture' ? 'image' : typeOf(asset) === 'Video' ? 'video' : 'music'" :size="17" /></span><div><code>&lt;{{ referenceOf(index) }}&gt;</code><b>{{ asset.display_name || asset.filename }}</b><small>顺序 {{ index + 1 }}</small></div><div><button type="button" title="上移" aria-label="上移" :disabled="index === 0" @click="moveAsset(index,-1)"><AppIcon name="chevronUp" :size="13" /></button><button type="button" title="下移" aria-label="下移" :disabled="index === assets.length - 1" @click="moveAsset(index,1)"><AppIcon name="chevronDown" :size="13" /></button><button type="button" title="移除" aria-label="移除" @click="store.removeGenerationAsset('video', selectedModelKey, asset.id)"><AppIcon name="close" :size="13" /></button></div></article><button type="button" class="asset-picker" @click="pickAssets"><AppIcon name="upload" :size="17" /><span><b>{{ assets.length ? '继续添加或拖入素材' : workflow === 'i2v' ? '选择或拖入一张首帧' : '添加或拖入参考素材' }}</b><small>{{ assets.length }} / {{ contract.maximum }} · {{ contract.accept.join('、') }}</small></span></button></div><p v-else class="video-no-source">无需素材，直接描述主体、动作、镜头、环境与声音。</p></div>
        <div class="video-block surface"><header><div><small>DIRECTION</small><h2>{{ capability.prompt_label || '视频描述' }}</h2></div><button v-if="workflow === 'ref2va'" type="button" class="link-button" @click="insertReference">插入素材引用</button></header><ol v-if="capability.workflow_guide?.steps?.length" class="workflow-steps"><li v-for="step in capability.workflow_guide.steps" :key="step">{{ step }}</li></ol><textarea v-model="prompt" rows="6" maxlength="4000" required :placeholder="capability.prompt_placeholder" /><button v-if="capability.workflow_guide?.prompt_template" type="button" class="link-button template-button" @click="useTemplate">使用示例模板</button></div>
        <div class="video-block surface"><header><div><small>OUTPUT</small><h2>生成设置</h2></div></header><OptionFields :fields="basicFields" :values="options" /><details v-if="advancedFields.length" class="advanced-fields"><summary>高级参数</summary><OptionFields :fields="advancedFields" :values="options" /></details></div>
        <div v-if="capability.notices?.length" class="contract-notices"><p v-for="notice in capability.notices" :key="notice">{{ notice }}</p></div>
      </section>
      <footer class="video-submit"><div class="video-preflight"><span><small>工作流</small><b>{{ workflowName }}</b></span><span><small>输出</small><b>{{ size }} · {{ duration }}</b></span><span><small>输入</small><b :class="{ warn: !inputValid }">{{ inputValid ? `${assets.length} 个素材，合同有效` : `需要 ${contract.minimum}–${contract.maximum} 个` }}</b></span></div><ActionButton action-key="submit:video" icon="play" label="生成视频" tone="primary" :disabled="!ready || !prompt.trim()" @click="submit" /></footer>
    </form>
    <TaskMonitor :task="task" kind="video" @reuse="reuse" />
  </section>
</template>
