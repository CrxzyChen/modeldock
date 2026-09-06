<script setup lang="ts">
defineProps<{ label: string; tone?: "ready" | "busy" | "error" | "neutral" | "warn" }>();
</script>

<template><span class="state-badge" :class="tone || 'neutral'"><i />{{ label }}</span></template>
