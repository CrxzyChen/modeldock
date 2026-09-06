<script setup lang="ts">
import { iconPaths, type IconName } from "@/lib/icons";

withDefaults(defineProps<{ name: IconName; size?: number }>(), { size: 18 });
</script>

<template>
  <svg class="app-icon" viewBox="0 0 24 24" :width="size" :height="size" aria-hidden="true">
    <path v-for="path in (iconPaths[name] ?? iconPaths.models)" :key="path" :d="path" />
  </svg>
</template>
