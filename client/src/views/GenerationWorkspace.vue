<script setup lang="ts">
import { computed, reactive, ref, watch } from "vue";
import ActionButton from "@/components/ActionButton.vue";
import AppIcon from "@/components/AppIcon.vue";
import OptionFields from "@/components/OptionFields.vue";
import StateBadge from "@/components/StateBadge.vue";
import TaskMonitor from "@/components/TaskMonitor.vue";
import { mediaNames } from "@/lib/format";
import { useAppStore } from "@/stores/app";
import type { MediaKind, ServiceModel } from "@/types/contracts";

const props = defineProps<{ kind: "speech" | "music" }>();
const store = useAppStore();
const selectedModelKey = ref("");
const prompt = ref("");
const dropActive = ref(false);
const optionValues = reactive<Record<string, string | number>>({});
const service = computed(() => store.services.find(item => item.kind === props.kind));
const models = computed(() => service.value?.models ?? []);
const model = computed(() => models.value.find(item => item.model_key === selectedModelKey.value) ?? null);
const capability = computed<Record<string, any>>(() => model.value?.capabilities ?? { options: [] });
const basicFields = computed(() => (capability.value.options ?? []).filter((field: any) => !field.advanced));
const advancedFields = computed(() => (capability.value.options ?? []).filter((field: any) => field.advanced));
const contract = computed(() => capability.value.input_contract ?? { minimum: 0, maximum: 0, accept: [] });
const assets = computed(() => store.generationInputs[store.generationInputKey(props.kind, selectedModelKey.value)] ?? []);
const task = computed(() => store.selectedTask(props.kind));
const ready = computed(() => Boolean(service.value?.enabled && model.value?.healthy));

function chooseDefault() {
  if (models.value.some(item => item.model_key === selectedModelKey.value)) return;
  selectedModelKey.value = models.value.find(item => item.model_key === service.value?.default_model && item.healthy)?.model_key
    ?? models.value.find(item => item.healthy)?.model_key ?? models.value[0]?.model_key ?? "";
}
function resetOptions(selected: ServiceModel | null) {
  Object.keys(optionValues).forEach(key => delete optionValues[key]);
  (selected?.capabilities?.options ?? []).forEach((field: any) => { optionValues[field.key] = field.default; });
}
watch(models, chooseDefault, { immediate: true });
watch(model, value => resetOptions(value), { immediate: true });
async function pickAssets() { if (model.value) await store.uploadPickedAssets(props.kind, contract.value.maximum > 1, contract.value.maximum, model.value.model_key, contract.value.accept); }
async function dropAssets(event: DragEvent) {
  dropActive.value = false;
  if (!model.value || !event.dataTransfer?.files.length) return;
  await store.uploadDroppedAssets(props.kind, [...event.dataTransfer.files], contract.value.maximum, model.value.model_key, contract.value.accept);
}
async function submit() {
  if (!model.value || !prompt.value.trim()) return;
  const result = await store.submitTask(props.kind, model.value.model_key, prompt.value.trim(), { ...optionValues }, assets.value.map(item => item.id));
  if (result) { prompt.value = ""; store.generationInputs[store.generationInputKey(props.kind, model.value.model_key)] = []; }
}
</script>

<template>
  <section class="workbench-view generation-workspace">
    <div class="generation-editor surface">
      <header class="workspace-header"><div><small>{{ kind.toUpperCase() }} WORKSPACE</small><h1>{{ mediaNames[kind] }}</h1></div><StateBadge :label="ready ? '模型就绪' : '服务离线'" :tone="ready ? 'ready' : 'error'" /></header>
      <form @submit.prevent="submit">
        <label class="field-block"><span>运行模型</span><select v-model="selectedModelKey"><option v-for="item in models" :key="item.model_key" :value="item.model_key" :disabled="!item.healthy">{{ item.label }}{{ item.healthy ? '' : ' · 离线' }}</option></select></label>
        <p class="model-contract">{{ model ? `${model.model_id ?? model.model_key} · 修订 ${model.revision ?? '—'}${model.gpu_indices?.length ? ` · GPU ${model.gpu_indices.join(' + ')}` : ''}` : '当前没有模型' }}</p>
        <button v-if="contract.maximum" type="button" class="asset-picker" :class="{ 'drop-active': dropActive }" @click="pickAssets" @dragenter.prevent="dropActive = true" @dragover.prevent @dragleave.self="dropActive = false" @drop.prevent.stop="dropAssets"><AppIcon name="upload" :size="17" /><span><b>添加或拖入参考素材</b><small>{{ assets.length }} / {{ contract.maximum }} · {{ contract.accept.join('、') }}</small></span></button>
        <div v-if="assets.length" class="asset-chips"><span v-for="asset in assets" :key="asset.id">{{ asset.display_name || asset.filename }}<button type="button" aria-label="移除素材" title="移除素材" @click="store.removeGenerationAsset(kind, selectedModelKey, asset.id)"><AppIcon name="close" :size="12" /></button></span></div>
        <label class="field-block prompt-field"><span>{{ capability.prompt_label || '生成提示词' }}</span><textarea v-model="prompt" rows="7" maxlength="4000" required :placeholder="capability.prompt_placeholder || '描述生成内容…'" /></label>
        <OptionFields :fields="basicFields" :values="optionValues" />
        <details v-if="advancedFields.length" class="advanced-fields"><summary>高级参数</summary><OptionFields :fields="advancedFields" :values="optionValues" /></details>
        <div v-if="capability.notices?.length" class="contract-notices"><p v-for="notice in capability.notices" :key="notice">{{ notice }}</p></div>
        <footer><small>{{ model?.gpu_indices?.length ? `部署 GPU ${model.gpu_indices.join(' + ')}` : '按实例配置原子调度' }}</small><ActionButton :action-key="`submit:${kind}`" icon="play" :label="`提交${mediaNames[kind]}任务`" tone="primary" :disabled="!ready || !prompt.trim()" @click="submit" /></footer>
      </form>
    </div>
    <TaskMonitor :task="task" :kind="kind" />
  </section>
</template>
