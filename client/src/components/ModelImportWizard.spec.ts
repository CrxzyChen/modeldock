import { beforeEach, describe, expect, it, vi } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import ModelImportWizard from "./ModelImportWizard.vue";
import { useAppStore } from "@/stores/app";

const mocks = vi.hoisted(() => ({ api: vi.fn(), list: vi.fn(), resume: vi.fn(), pause: vi.fn() }));
vi.mock("@/services/api", () => ({ api: mocks.api }));
vi.mock("@/services/desktop", () => ({ desktopBridge: () => ({
  listModelUploadSessions: mocks.list, resumeModelUpload: mocks.resume, pauseModelUpload:mocks.pause,
}) }));
const saved = { id: "upload-one", profileId: "server-a", state: "paused", selection: "file",
  format: "safetensors", displayName: "model", createdAt: "2026-09-06T00:00:00Z",
  updatedAt: "2026-09-06T00:00:00Z", totalBytes: 1024, transferId: "transfer-one",
  files: [{relativePath: "model.safetensors", size:1024, sha256:"a".repeat(64)}],
  preview:{role:"checkpoint", architectureFamily:"unknown", precision:"unknown", tensorCount:1},
  reusedAsset:null, error:null };
const transfer = { id:"transfer-one", direction:"upload", state:"paused", received_bytes:512,
  expected_bytes:1024, display_name:"bound name", role:"checkpoint", revision:"bound-revision",
  license_declared:"unknown" };
function setup() {
  const store = useAppStore();
  store.connections = { activeProfileId:"server-a", profiles:[{id:"server-a",name:"A"}] } as any;
  return { store, wrapper: mount(ModelImportWizard, {props:{open:true}, global:{stubs:{teleport:true}}}) };
}
describe("ModelImportWizard resume", () => {
  beforeEach(() => { localStorage.clear(); setActivePinia(createPinia()); vi.clearAllMocks();
    mocks.list.mockResolvedValue([saved]); mocks.api.mockResolvedValue(transfer); });
  it.each([{id:'wrong'}, {expected_bytes:2048}, {revision:null}, {display_name:null},
    {role:'unexpected'}, {license_declared:42}])('rejects invalid bound metadata %j', async (change) => {
    mocks.api.mockResolvedValue({...transfer,...change});
    const {wrapper}=setup(); await flushPromises();
    await wrapper.get('.resume-sessions article button').trigger('click'); await flushPromises();
    expect(wrapper.get('[role=alert]').text()).toContain('原传输信息不匹配');
    expect(mocks.resume).not.toHaveBeenCalled(); wrapper.unmount();
  });
  it("restores bound metadata and bytes using one GET, with no upload", async () => {
    const {wrapper}=setup(); await flushPromises();
    await wrapper.get('.resume-sessions article button').trigger('click'); await flushPromises();
    expect(mocks.api).toHaveBeenCalledExactlyOnceWith('/api/v1/model-transfers/transfer-one');
    expect(wrapper.get('.import-live-progress').text()).toContain('50%');
    expect(wrapper.get('.import-live-progress').text()).toContain('上传已暂停');
    expect(wrapper.text()).toContain('继续上传');
    const fields=wrapper.findAll('.inspection-form input');
    expect((fields[1]!.element as HTMLInputElement).value).toBe('bound-revision');
    expect(fields[1]!.attributes('disabled')).toBeDefined();
    expect(mocks.resume).not.toHaveBeenCalled(); wrapper.unmount();
  });
  it("discards a late response after switching server", async () => {
    let resolve!: (value:any)=>void;
    mocks.api.mockImplementation(() => new Promise(done => {resolve=done;}));
    const {store,wrapper}=setup(); await flushPromises();
    await wrapper.get('.resume-sessions article button').trigger('click');
    store.connectionEpoch++; await flushPromises(); resolve(transfer); await flushPromises();
    expect(wrapper.find('.import-live-progress').exists()).toBe(false);
    expect(wrapper.text()).not.toContain('bound name');
    expect(mocks.resume).not.toHaveBeenCalled(); wrapper.unmount();
  });
  it("keeps the new session when an older same-server read completes late", async () => {
    const second={...saved,id:'upload-two',transferId:'transfer-two',displayName:'second'};
    mocks.list.mockResolvedValue([saved,second]);
    let old!: (value:any)=>void;
    mocks.api.mockImplementation((url:string) => url.endsWith('transfer-one')
      ? new Promise(done => {old=done;})
      : Promise.resolve({...transfer,id:'transfer-two',revision:'second-revision',received_bytes:256}));
    const {wrapper}=setup(); await flushPromises();
    await wrapper.get('.resume-sessions article button').trigger('click');
    await wrapper.findAll('button').find(button => button.text()==='上一步')!.trigger('click');
    await wrapper.findAll('.resume-sessions article')[1]!.get('button').trigger('click');
    await flushPromises(); old(transfer); await flushPromises();
    expect(wrapper.get('.import-live-progress').text()).toContain('25%');
    expect((wrapper.findAll('.inspection-form input')[1]!.element as HTMLInputElement).value).toBe('second-revision');
    wrapper.unmount();
  });
  it("retains paused UI after pause response even if a chunk event arrives late", async () => {
    mocks.list.mockResolvedValue([{...saved,state:'uploading'}]);
    mocks.pause.mockResolvedValue({session:saved,transfer});
    const {store,wrapper}=setup(); await flushPromises();
    await wrapper.get('.resume-sessions article button').trigger('click'); await flushPromises();
    await wrapper.findAll('button').find(button => button.text()==='暂停')!.trigger('click');
    await flushPromises();
    store.modelImportProgress[saved.id]={profileId:'server-a',revision:1,stage:'uploading',
      relativePath:'model.safetensors',receivedBytes:768,totalBytes:1024};
    await flushPromises();
    expect(wrapper.get('.import-live-progress').text()).toContain('上传已暂停');
    expect(wrapper.findAll('button').some(button => button.text()==='暂停')).toBe(false);
    expect(mocks.pause).toHaveBeenCalledExactlyOnceWith(saved.id); wrapper.unmount();
  });
  it("resumes with the bound revision and retires stale paused progress", async () => {
    let finish!: (value:any)=>void;
    mocks.resume.mockImplementation(() => new Promise(done => {finish=done;}));
    const {store,wrapper}=setup(); await flushPromises();
    await wrapper.get('.resume-sessions article button').trigger('click'); await flushPromises();
    await wrapper.findAll('button').find(button => button.text().includes('继续上传'))!.trigger('click');
    expect(mocks.resume).toHaveBeenCalledWith(expect.objectContaining({
      sessionId:'upload-one', revision:'bound-revision', displayName:'bound name',
    }));
    store.modelImportProgress['upload-one']={profileId:'server-a',revision:1,sessionId:'upload-one',
      transferId:'transfer-one', stage:'uploading',relativePath:'model.safetensors',receivedBytes:768,totalBytes:1024};
    await flushPromises();
    expect(wrapper.get('.import-live-progress').text()).toContain('75%');
    expect(wrapper.get('.import-live-progress').text()).not.toContain('已暂停');
    finish({disposition:'paused',session:saved}); await flushPromises();
    expect(wrapper.get('.import-live-progress').text()).toContain('已暂停');
    wrapper.unmount();
  });
  it("shows retry-read on failure rather than starting a replacement upload", async () => {
    mocks.api.mockRejectedValue(new Error('offline'));
    const {wrapper}=setup(); await flushPromises();
    await wrapper.get('.resume-sessions article button').trigger('click'); await flushPromises();
    expect(wrapper.get('[role=alert]').text()).toContain('无法读取原传输');
    expect(wrapper.text()).toContain('重新读取');
    await wrapper.findAll('button').find(button => button.text().includes('重新读取'))!.trigger('click');
    await flushPromises();
    expect(mocks.api).toHaveBeenCalledTimes(2);
    expect(mocks.resume).not.toHaveBeenCalled(); wrapper.unmount();
  });
});
