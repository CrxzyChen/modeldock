from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ElectronClientTests(unittest.TestCase):
    def source(self, path: str) -> str:
        return (ROOT / path).read_text(encoding="utf-8")

    def test_upload_pause_drains_inflight_chunk_and_preserves_resume_identity(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is required to execute Electron upload state-machine code")
        script = r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
const source = require('node:fs').readFileSync('electron/main.js', 'utf8');
const implementation = source.slice(source.indexOf('async function uploadModelSessionFiles('), source.indexOf('async function pickAndUploadModel('));
async function scenario(initialOffset = 0, pauseFailure = false) {
  const selected = {id:'s',profileId:'p',state:'failed',format:'safetensors',transferId:'original',preview:{role:'checkpoint'},
    files:[{absolutePath:'approved',relativePath:'model.safetensors',size:12,mtimeMs:1,sha256:'abc'}]};
  const transfer = {id:'original',state:'transferring',max_chunk_bytes:4,
    files:[{id:'f',relative_path:'model.safetensors',expected_bytes:12,received_bytes:initialOffset}]};
  const snapshot = {profile:{id:'p'},revision:1,key:'test'};
  let releaseChunk, gate = new Promise(resolve=>{releaseChunk=resolve}), failGet=false, pauseHold=null, cancelHold=null;
  const puts=[], calls=[], states=[], controls = new Map();
  const context = {
    Buffer, URL, modelUploadRuns:controls, modelUploadSessions:{sessions:[selected]},
    fs:{promises:{stat:async()=>({isFile:()=>true,size:12,mtimeMs:1}),open:async()=>({read:async(buffer)=>({bytesRead:buffer.length}),close:async()=>{}})}},
    net:{fetch:async(url,options)=>{
      const offset=Number(new URL(url).searchParams.get('offset'));
      puts.push(offset); await gate;
      assert.equal(transfer.state,'transferring','PUT reached a paused transfer');
      assert.equal(offset,transfer.files[0].received_bytes);
      transfer.files[0].received_bytes+=options.body.length;
      return {ok:true,json:async()=>({received_bytes:transfer.files[0].received_bytes})};
    }},
    apiUrl:raw=>new URL(raw,'http://test.local'), assertConnectionCurrent:()=>{}, mainWindow:null,
    assertTrustedSender:()=>{}, activeConnectionSnapshot:()=>snapshot,
    requireModelUploadSession:()=>selected, publicModelUploadSession:value=>({...value}),
    updateModelUploadSession:(value,changes)=>{Object.assign(value,changes);states.push(changes.state)},
    saveModelUploadSessions:()=>{},
    modelJsonRequest:async(_snapshot,url)=>{
      calls.push(url);
      if(url.endsWith('/preflight')) return {disposition:'upload'};
      if(url.endsWith('/original')) { if(failGet) throw new Error('transient observation'); return structuredClone(transfer); }
      if(url.endsWith('/pause')) { if(pauseHold) await pauseHold; if(pauseFailure) throw new Error('pause offline'); transfer.state='paused'; return structuredClone(transfer); }
      if(url.endsWith('/resume')) { transfer.state='transferring'; return structuredClone(transfer); }
      if(url.endsWith('/complete')) {transfer.state='succeeded';return structuredClone(transfer);}
      if(url.endsWith('/cancel')) {if(cancelHold) await cancelHold;return {};}
      throw new Error('Unexpected request '+url);
    },
  };
  const methods=vm.runInNewContext(implementation+'\n({startModelUpload,pauseModelUpload,discardModelUpload})',context);
  const uploading=methods.startModelUpload({}, {sessionId:'s'});
  for(let count=0;!puts.length&&count<100;count++) await new Promise(setImmediate);
  assert.equal(puts.length,1);
  await assert.rejects(methods.startModelUpload({}, {sessionId:'s'}),/仍在执行/);
  await assert.rejects(methods.discardModelUpload({},'s'),/先暂停/);
  const pausing=methods.pauseModelUpload({},'s');
  const again=methods.pauseModelUpload({},'s');
  // Attach rejection handlers before resolving the intentionally blocked I/O.
  const results=Promise.allSettled([uploading,pausing,again]);
  await new Promise(setImmediate);
  assert.equal(calls.filter(url=>url.endsWith('/pause')).length,0);
  releaseChunk();
  const settled=await results;
  assert.equal(puts.length,1,'Producer did not stop at a chunk boundary');
  assert.equal(calls.filter(url=>url.endsWith('/complete')).length,0);
  assert.equal(calls.filter(url=>url.endsWith('/pause')).length,1);
  assert.equal(controls.size,0);
  if(pauseFailure) {
    assert(settled.every(item=>item.status==='rejected'));
    assert.equal(selected.state,'failed');
    assert.equal(selected.transferId,'original');
    return;
  }
  assert(settled.every(item=>item.status==='fulfilled'));
  assert.equal(settled[0].value.disposition,'paused');
  assert.equal(selected.state,'paused');
  assert.equal(selected.transferId,'original');
  assert(!states.includes('server-verifying'));
  const resumed=await methods.startModelUpload({}, {sessionId:'s'});
  assert.equal(resumed.disposition,'uploaded');
  assert.equal(selected.state,'completed');
  assert.equal(transfer.files[0].received_bytes,12);
  assert.deepEqual(puts,initialOffset===0?[0,4,8]:[8]);
  assert.equal(calls.filter(url=>url.endsWith('/complete')).length,1);
  failGet=true;
  await assert.rejects(methods.startModelUpload({}, {sessionId:'s'}),/transient observation/);
  assert.equal(selected.transferId,'original','Observation failure discarded transfer identity');
  failGet=false;
  let releasePause;
  pauseHold=new Promise(resolve=>{releasePause=resolve});
  const idlePause=methods.pauseModelUpload({},'s');
  const idlePauseAgain=methods.pauseModelUpload({},'s');
  await assert.rejects(methods.startModelUpload({}, {sessionId:'s'}),/仍在执行/);
  await assert.rejects(methods.discardModelUpload({},'s'),/先暂停/);
  releasePause(); await Promise.all([idlePause,idlePauseAgain]);
  assert.equal(controls.size,0);
  let releaseCancel;
  cancelHold=new Promise(resolve=>{releaseCancel=resolve});
  const discarding=methods.discardModelUpload({},'s');
  await assert.rejects(methods.startModelUpload({}, {sessionId:'s'}),/仍在执行/);
  await assert.rejects(methods.pauseModelUpload({},'s'),/其他操作/);
  releaseCancel(); await discarding;
  assert.equal(context.modelUploadSessions.sessions.length,0);
  assert.equal(controls.size,0);
}
(async()=>{
  await scenario();
  await scenario(8);
  await scenario(0,true);
  console.log('pause drain, duplicate commands, resume offsets, last chunk, observation failure, pause failure: passed');
})().catch(error=>{console.error(error);process.exitCode=1});
'''
        result = subprocess.run([node, "-e", script], cwd=ROOT, capture_output=True,
                                text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("passed", result.stdout)

    def test_renderer_is_vue_build_loaded_through_bounded_protocol(self) -> None:
        main = self.source("electron/main.js")
        server = self.source("mediacenter/server.py")
        html = self.source("client/index.html")
        package = json.loads(self.source("package.json"))
        self.assertIn('const RENDERER_ROOT = path.resolve(__dirname, "..", "client", "dist")', main)
        self.assertIn('const APP_URL = `${RENDERER_SCHEME}://app/index.html`', main)
        self.assertIn("protocol.registerSchemesAsPrivileged", main)
        self.assertIn("protocol.handle(RENDERER_SCHEME, handleRendererRequest)", main)
        self.assertIn('url.hostname !== "app"', main)
        self.assertIn("target.startsWith(`${RENDERER_ROOT}${path.sep}`)", main)
        self.assertIn("fs.readFileSync(target)", main)
        self.assertIn('"Content-Type": contentType', main)
        self.assertNotIn("pathToFileURL", main)
        self.assertIn("await window.loadURL(APP_URL)", main)
        self.assertIn('http-equiv="Content-Security-Policy"', html)
        self.assertEqual(package["build"]["files"], ["electron/**/*", "client/dist/**/*", "package.json"])
        self.assertNotIn("mediacenter/static", main + json.dumps(package))
        self.assertNotIn("STATIC_ROOT", server)
        self.assertIn("desktop_client_required", server)

    def test_vue_typescript_pinia_toolchain_is_the_only_renderer(self) -> None:
        package = json.loads(self.source("package.json"))
        self.assertNotIn("dependencies", package)
        for dependency in ("vue", "pinia", "vite", "typescript", "vue-tsc", "vitest", "@vue/test-utils"):
            self.assertIn(dependency, package["devDependencies"])
        self.assertIn("vite build", package["scripts"]["build:client"])
        self.assertIn("vue-tsc --noEmit", package["scripts"]["typecheck"])
        app = self.source("client/src/App.vue")
        for view in ("OverviewView", "ImageWorkspace", "VideoWorkspace", "GenerationWorkspace",
                     "ServicesView", "ModelCenterView", "HardwareView", "AuditView"):
            self.assertIn(view, app)

    def test_browser_security_window_state_and_native_io_remain_bounded(self) -> None:
        main = self.source("electron/main.js")
        preload = self.source("electron/preload.js")
        for value in ("contextIsolation: true", "nodeIntegration: false", "sandbox: true",
                      "setPermissionRequestHandler", 'setWindowOpenHandler(() => ({ action: "deny" }))',
                      'window.webContents.on("will-redirect", event => event.preventDefault())'):
            self.assertIn(value, main)
        self.assertIn('const WINDOW_STATE_FILE = "window-state.json"', main)
        self.assertIn("screen.getAllDisplays()", main)
        self.assertIn("saveWindowState(window)", main)
        self.assertIn("frame: false", main)
        self.assertIn('ipcMain.handle("window:control", controlWindow)', main)
        self.assertIn("dialog.showOpenDialog(mainWindow", main)
        self.assertIn("dialog.showSaveDialog(mainWindow", main)
        self.assertIn('const MAX_ASSET_BYTES = 256 * 1024 * 1024', main)
        self.assertNotIn('require("node:fs")', preload)

    def test_credentials_connections_and_source_tokens_stay_in_main_process(self) -> None:
        main = self.source("electron/main.js")
        preload = self.source("electron/preload.js")
        renderer = self.source("client/src/stores/app.ts") + self.source("client/src/views/LoginView.vue")
        self.assertIn("safeStorage.encryptString", main)
        self.assertIn("safeStorage.decryptString", main)
        self.assertIn("assertTrustedSender(event)", main)
        self.assertIn('ipcMain.handle("api:request", proxyApi)', main)
        for bridge in ("connections:list", "connections:connect", "connections:switch",
                       "connections:logout", "connections:remove"):
            self.assertIn(bridge, preload)
        self.assertNotIn("connectionCredentials", preload + renderer)
        self.assertNotIn("sourceCredentials", preload + renderer)
        self.assertIn('ipcMain.handle("source-authorization:configure"', main)
        self.assertIn("sourceCredentialTransportAllowed", main)
        self.assertIn("allowInsecureSourceAuthorization", renderer)

    def test_sse_is_main_process_owned_without_renderer_polling(self) -> None:
        main = self.source("electron/main.js")
        preload = self.source("electron/preload.js")
        store = self.source("client/src/stores/app.ts")
        self.assertIn('apiUrl("/api/v1/events", snapshot)', main)
        self.assertIn('"Last-Event-ID"', main)
        self.assertIn("serverEventController.abort()", main)
        self.assertIn('ipcMain.handle("events:start", startServerEvents)', main)
        self.assertIn('ipcRenderer.on("server:event"', preload)
        self.assertIn("handleServerEvent", store)
        self.assertIn('event.type === "gpu.telemetry"', store)
        self.assertNotIn("setInterval(", store)

    def test_model_import_stays_in_main_process_and_resumes_per_server(self) -> None:
        main = self.source("electron/main.js")
        preload = self.source("electron/preload.js")
        types = self.source("client/src/env.d.ts")
        self.assertIn('const MODEL_UPLOAD_SESSIONS_FILE = "model-upload-sessions.json"', main)
        self.assertIn("createHash(\"sha256\")", main)
        self.assertIn("inspectLocalSafetensors", main)
        self.assertIn("inferDirectoryPreview", main)
        self.assertIn("saveModelUploadSessions()", main)
        self.assertIn("mode: 0o600", main)
        self.assertIn('modelJsonRequest(snapshot, "/api/v1/model-imports/preflight", "POST"', main)
        self.assertIn('`/api/v1/model-transfers/${session.transferId}`', main)
        self.assertIn("serverFile.received_bytes || 0", main)
        # Chromium computes this header from the Buffer; explicitly forwarding
        # it makes actual net.fetch reject the chunk before it reaches HTTP.
        self.assertNotIn('"Content-Length": String(bytesRead)', main)
        for channel in (
            "model:pick-for-import", "model:upload-sessions", "model:start-upload",
            "model:resume-upload", "model:pause-upload", "model:discard-upload",
        ):
            self.assertIn(f'ipcMain.handle("{channel}"', main)
            self.assertIn(channel, preload)
        for method in (
            "pickModelForImport", "listModelUploadSessions", "startModelUpload",
            "resumeModelUpload", "pauseModelUpload", "discardModelUpload",
        ):
            self.assertIn(method, preload)
            self.assertIn(method, types)
        self.assertNotIn("absolutePath", preload + types)

    def test_renderer_can_only_forward_a_bounded_deployment_idempotency_key(self) -> None:
        main = self.source("electron/main.js")
        preload = self.source("electron/preload.js")
        api = self.source("client/src/services/api.ts")
        self.assertIn("idempotencyKey: options.idempotencyKey", preload)
        self.assertIn("idempotencyKey?: string", api)
        self.assertIn('/^[A-Za-z0-9._:-]{1,128}$/u', main)
        self.assertIn('headers["Idempotency-Key"] = idempotencyKey', main)
        self.assertIn('method !== "POST"', main)

    def test_renderer_typography_uses_midnight_workshop_scale(self) -> None:
        base = self.source("client/src/styles/base.css")
        self.assertIn("--font-caption: 10px", base)
        self.assertIn("--font-chrome: 11px", base)
        self.assertIn("--font-control: 12px", base)
        self.assertIn("--font-body: 13px", base)
        self.assertIn("font-size: var(--font-body)", base)
        for stylesheet in (ROOT / "client/src/styles").glob("*.css"):
            source = stylesheet.read_text(encoding="utf-8")
            literal_sizes = [
                float(value)
                for value in re.findall(r"(?:font-size|font)\s*:\s*([0-9.]+)px", source)
            ]
            self.assertFalse(
                [value for value in literal_sizes if value < 8],
                f"{stylesheet.name} contains text below the 8px absolute accessibility floor",
            )
        smoke = self.source("tests/electron-smoke.js")
        self.assertIn("undersized visible text", smoke)
        self.assertIn("minVisibleFontPx:10", smoke)

    def test_renderer_uses_midnight_workshop_theme(self) -> None:
        main = self.source("client/src/main.ts")
        theme = self.source("client/src/styles/theme.css")
        base = self.source("client/src/styles/base.css")
        shell = self.source("client/src/styles/shell.css")
        self.assertLess(main.index('import "./styles/image.css"'), main.index('import "./styles/theme.css"'))
        self.assertIn('document.documentElement.dataset.theme = "midnight-workshop"', main)
        for token in (
            "--mw-canvas: #090b10",
            "--mw-surface: #0d1118",
            "--mw-surface-raised: #111721",
            "--mw-surface-overlay: #151c27",
            "--mw-accent: #49cddd",
            "--mw-text: #c9d1d9",
            "--mw-text-muted: #728091",
            "--font-caption: 10px",
            "--font-body: 13px",
            "--radius-control: var(--mw-radius-control)",
            "--chrome-title: 38px",
            "--chrome-sidebar: 48px",
            "--chrome-status: 26px",
            "--mw-panel-inset: 12px",
            "--mw-group-gap: 10px",
            "--space-panel: var(--mw-panel-inset)",
        ):
            self.assertIn(token, base)
        self.assertIn(".sidebar-item.active", shell)
        self.assertIn(".model-row.active", theme)
        self.assertIn(".image-stage", theme)
        self.assertIn(".state-badge.ready", theme)
        self.assertIn(".state-badge.busy", theme)
        self.assertIn("/* Midnight Workshop product skin.", theme)
        self.assertIn("grid-template-rows: 34px 36px minmax(0, 1fr) 110px", theme)
        self.assertNotIn("gradient(", theme)
        self.assertNotIn("gradient(", self.source("client/src/styles/shell.css"))
        self.assertIn("stroke-width: 1.35", self.source("client/src/styles/shell.css"))

    def test_midnight_workshop_v2_uses_continuous_work_surfaces(self) -> None:
        theme = self.source("client/src/styles/theme.css")
        overview = self.source("client/src/views/OverviewView.vue")
        services = self.source("client/src/views/ServicesView.vue")
        models = self.source("client/src/views/ModelCenterView.vue")
        self.assertIn("overview-work-state", overview)
        self.assertIn("service-config-list", services)
        self.assertNotIn("service-config-card", services)
        self.assertIn("model-runtime-summary", models)
        self.assertNotIn("model-status-board", models)
        for selector in (".overview-work-state", ".service-config-row", ".model-runtime-summary", ".video-block.surface"):
            self.assertIn(selector, theme)

    def test_image_history_and_model_picker_preserve_compact_information(self) -> None:
        image = self.source("client/src/views/ImageWorkspace.vue")
        theme = self.source("client/src/styles/theme.css")
        icons = self.source("client/src/lib/icons.ts")
        self.assertIn('class="image-model-picker"', image)
        self.assertIn('role="listbox" aria-label="生成模型"', image)
        self.assertIn('name="models" :size="13"', image)
        self.assertIn('name="edit" :size="13"', image)
        self.assertIn('name="fit" :size="13"', image)
        self.assertNotIn("<span>01</span>", image)
        self.assertNotIn("<span>02</span>", image)
        self.assertNotIn("<span>03</span>", image)
        self.assertIn('edit: ["M4 20h4l11-11-4-4L4 16v4Z"', icons)
        self.assertIn("padding: var(--mw-space-2) var(--mw-space-3) 12px", theme)
        self.assertIn("flex-basis: 156px", theme)
        self.assertIn("height: 64px", theme)
        self.assertIn("min-height: 30px", theme)
        self.assertIn("grid-template-columns: 15px minmax(0, 1fr) 7px", theme)

    def test_windows_distribution_is_pinned_and_self_contained(self) -> None:
        package = json.loads(self.source("package.json"))
        from mediacenter.release_version import RELEASE_VERSION
        self.assertEqual(package["version"], RELEASE_VERSION)
        self.assertIn(f'version = "{RELEASE_VERSION}"', self.source("pyproject.toml"))
        self.assertEqual(package["devDependencies"]["electron"], "44.0.0")
        self.assertEqual(package["devDependencies"]["electron-builder"], "26.15.3")
        self.assertEqual(package["build"]["win"]["target"], ["nsis"])
        self.assertTrue((ROOT / "client/dist/index.html").is_file())
        self.assertTrue((ROOT / "client/dist/assets/app.js").is_file())
        self.assertTrue((ROOT / "client/dist/assets/app.css").is_file())


if __name__ == "__main__":
    unittest.main()
