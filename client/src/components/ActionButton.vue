<script setup lang="ts">
import { computed } from "vue";
import AppIcon from "./AppIcon.vue";
import { useAppStore } from "@/stores/app";
import type { IconName } from "@/lib/icons";

const props = withDefaults(defineProps<{
  actionKey?: string; icon?: IconName; label?: string; title?: string; disabled?: boolean;
  tone?: "normal" | "primary" | "danger"; compact?: boolean
}>(), { actionKey: "", label: "", title: "", tone: "normal", compact: false });
defineEmits<{ click: [event: MouseEvent] }>();
const store = useAppStore();
const action = computed(() => props.actionKey ? store.actionStates[props.actionKey] : undefined);
const pending = computed(() => action.value?.phase === "pending");
</script>

<template>
  <button
    type="button"
    class="action-button"
    :class="[`tone-${tone}`, { compact }]"
    :disabled="disabled || pending"
    :aria-busy="pending"
    :title="title || label"
    @click="$emit('click', $event)"
  >
    <AppIcon v-if="icon" :name="icon" :size="compact ? 15 : 17" />
    <span v-if="label" class="action-button-label">{{ label }}</span>
    <i v-if="pending" class="action-spinner" aria-hidden="true" />
    <AppIcon v-else-if="action?.phase === 'success'" class="action-success" name="check" :size="13" />
    <span class="sr-only" aria-live="polite">{{ action?.label }}</span>
  </button>
</template>
