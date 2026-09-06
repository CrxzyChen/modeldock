<script setup lang="ts">
import { computed } from "vue";
const props = defineProps<{ label: string; value?: number | null; detail?: string }>();
const safe = computed(() => Math.max(0, Math.min(100, Math.round(Number(props.value ?? 0)))));
const tone = computed(() => props.value == null ? "unavailable" : safe.value >= 90 ? "critical" : safe.value >= 75 ? "warning" : "normal");
</script>
<template><div class="radial-gauge" :class="tone" :style="{ '--gauge': `${safe}%` }" role="img" :aria-label="`${label} ${value == null ? '不可用' : `${safe}%`}`"><div><b>{{ value == null ? '—' : `${safe}%` }}</b><small>{{ label }}</small></div><span>{{ detail }}</span></div></template>
