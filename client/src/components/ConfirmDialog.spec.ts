import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mount } from '@vue/test-utils';
import { nextTick } from 'vue';
import { createPinia, setActivePinia } from 'pinia';
import ConfirmDialog from './ConfirmDialog.vue';
import { useAppStore } from '@/stores/app';

describe('ConfirmDialog', () => {
  beforeEach(() => {
    localStorage.clear(); setActivePinia(createPinia());
    // JSDOM has no top layer. These fixtures test lifecycle only; Electron
    // separately verifies hit testing, modal focus and keyboard confinement.
    Object.defineProperty(HTMLDialogElement.prototype, 'showModal', { configurable: true,
      value: vi.fn(function(this: HTMLDialogElement) { this.open = true; }) });
    Object.defineProperty(HTMLDialogElement.prototype, 'close', { configurable: true,
      value: vi.fn(function(this: HTMLDialogElement) { this.open = false; }) });
  });
  afterEach(() => {
    vi.restoreAllMocks(); document.body.replaceChildren();
    delete (HTMLDialogElement.prototype as Partial<HTMLDialogElement>).showModal;
    delete (HTMLDialogElement.prototype as Partial<HTMLDialogElement>).close;
  });

  it('uses the native modal layer and restores the triggering focus after cancel', async () => {
    const show = vi.spyOn(HTMLDialogElement.prototype, 'showModal');
    const trigger = document.createElement('button');
    document.body.append(trigger); trigger.focus();
    const wrapper = mount(ConfirmDialog, { attachTo: document.body });
    const store = useAppStore();
    const answer = store.confirm('卸载测试实例', '模型和历史保留', '确认卸载');
    await nextTick();
    const dialog = document.querySelector('dialog')!;
    expect(dialog).not.toBeNull();
    expect(show).toHaveBeenCalledTimes(1);
    expect(document.activeElement?.textContent).toBe('返回');
    dialog.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true }));
    expect(document.activeElement?.textContent).toBe('确认卸载');
    dialog.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true }));
    expect(document.activeElement?.textContent).toBe('返回');
    dialog.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', shiftKey: true, bubbles: true, cancelable: true }));
    expect(document.activeElement?.textContent).toBe('确认卸载');
    dialog.dispatchEvent(new Event('cancel', { cancelable: true }));
    await nextTick();
    expect(await answer).toBe(false);
    expect(dialog.open).toBe(false);
    expect(document.activeElement).toBe(trigger);
    wrapper.unmount();
  });

  it('resolves once and keeps a subsequent confirmation independent', async () => {
    const wrapper = mount(ConfirmDialog, { attachTo: document.body });
    const store = useAppStore();
    const first = store.confirm('第一次', '保留资产', '卸载');
    const apply = vi.fn();
    void first.then(accepted => { if (accepted) apply(); });
    await nextTick();
    document.querySelector<HTMLButtonElement>('.confirm-panel .danger')!.click();
    document.querySelector<HTMLButtonElement>('.confirm-panel .danger')!.click();
    await nextTick(); expect(await first).toBe(true);
    expect(apply).toHaveBeenCalledTimes(1);
    const second = store.confirm('第二次', '保留资产', '卸载');
    await nextTick(); expect(document.querySelector('dialog')!.open).toBe(true);
    wrapper.unmount(); expect(await second).toBe(false);
  });
});
