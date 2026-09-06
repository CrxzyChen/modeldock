<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue';
import AppIcon from "./AppIcon.vue";
import { useAppStore } from "@/stores/app";

const store = useAppStore();
const dialog = ref<HTMLDialogElement | null>(null);
const cancelButton = ref<HTMLButtonElement | null>(null);
let returnFocus: HTMLElement | null = null;

function cycleFocus(event: KeyboardEvent) {
  const buttons = [...(dialog.value?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)') || [])];
  if (!buttons.length) return;
  const index = buttons.indexOf(document.activeElement as HTMLButtonElement);
  event.preventDefault();
  buttons[(index + (event.shiftKey ? buttons.length - 1 : 1)) % buttons.length]?.focus();
}

function syncDialog() {
  if (!dialog.value) return;
  if (store.confirmation.open && !dialog.value.open) {
    returnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    dialog.value.showModal();
    cancelButton.value?.focus({ preventScroll: true });
  } else if (!store.confirmation.open && dialog.value.open) {
    dialog.value.close();
    if (returnFocus?.isConnected) returnFocus.focus({ preventScroll: true });
    returnFocus = null;
  }
}
watch(() => store.confirmation.open, syncDialog, { flush: 'post' });
onMounted(syncDialog);
onBeforeUnmount(() => {
  if (store.confirmation.open) store.answerConfirmation(false);
  syncDialog();
});
</script>

<template>
  <Teleport to="body">
      <dialog ref="dialog" class="confirmation-dialog" role="alertdialog" :aria-label="store.confirmation.title" aria-describedby="confirmation-message"
        @keydown.tab="cycleFocus" @cancel.prevent="store.answerConfirmation(false)" @pointerdown.self="store.answerConfirmation(false)">
        <section class="confirm-panel">
          <header><span class="confirm-mark"><AppIcon name="warning" :size="17" /></span><div><small>确认操作</small><h2>{{ store.confirmation.title }}</h2></div></header>
          <p id="confirmation-message">{{ store.confirmation.message }}</p>
          <footer>
            <button ref="cancelButton" type="button" class="flat-button" autofocus @click="store.answerConfirmation(false)">返回</button>
            <button type="button" class="flat-button" :class="store.confirmation.tone" @click="store.answerConfirmation(true)">{{ store.confirmation.confirmLabel }}</button>
          </footer>
        </section>
      </dialog>
  </Teleport>
</template>

<style scoped>
.confirmation-dialog {
  position: fixed;
  inset: 0;
  width: 100%;
  height: 100%;
  max-width: none;
  max-height: none;
  margin: 0;
  padding: 20px;
  border: 0;
  background: transparent;
  color: inherit;
}
.confirmation-dialog[open] { display: grid; place-items: center; }
.confirmation-dialog::backdrop { background: #0009; }
.confirm-panel { max-height: 100%; overflow: auto; }
</style>
