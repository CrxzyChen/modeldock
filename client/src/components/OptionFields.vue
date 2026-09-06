<script setup lang="ts">
defineProps<{ fields: Array<Record<string, any>>; values: Record<string, string | number> }>();
</script>

<template>
  <div class="option-fields">
    <label v-for="field in fields" :key="field.key" :class="{ wide: field.type === 'string' }">
      <span>{{ field.label }}</span>
      <textarea v-if="field.type === 'string'" v-model="values[field.key]" rows="2" :maxlength="field.max_length ?? 4000" :placeholder="field.placeholder" />
      <input v-else v-model.number="values[field.key]" type="number" :min="field.minimum" :max="field.maximum" :step="field.step ?? 1">
    </label>
  </div>
</template>
