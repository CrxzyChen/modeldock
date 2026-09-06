<script setup lang="ts">
defineProps<{ kicker: string; title: string; description?: string }>();
</script>

<template>
  <header class="page-header">
    <div class="page-heading"><small>{{ kicker }}</small><h1>{{ title }}</h1><p v-if="description">{{ description }}</p></div>
    <div class="page-header-actions"><slot /></div>
  </header>
</template>
