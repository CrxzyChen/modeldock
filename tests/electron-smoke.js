"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { pathToFileURL } = require("node:url");
const { app, BrowserWindow, net, protocol } = require("electron");

const ROOT = path.resolve(__dirname, "..");
const RENDERER_ROOT = path.join(ROOT, "client", "dist");
const APP_URL = "mediacenter-smoke://app/index.html";
const EVIDENCE_DIR = process.env.MEDIACENTER_SMOKE_EVIDENCE_DIR
  ? path.resolve(process.env.MEDIACENTER_SMOKE_EVIDENCE_DIR)
  : path.join(ROOT, "docs", "project-control", "evidence", "ph8-cp3");
const errors = [];

protocol.registerSchemesAsPrivileged([{ scheme: "mediacenter-smoke", privileges: { standard: true, secure: true, supportFetchAPI: true } }]);
app.setPath("userData", path.join(os.tmpdir(), "mediacenter-electron-smoke"));

function rendererPath(rawUrl) {
  const url = new URL(rawUrl);
  if (url.hostname !== "app") throw new Error("invalid renderer host");
  const target = path.resolve(RENDERER_ROOT, `.${decodeURIComponent(url.pathname || "/index.html")}`);
  if (target !== RENDERER_ROOT && !target.startsWith(`${RENDERER_ROOT}${path.sep}`)) throw new Error("renderer path escape");
  return target;
}

async function waitFor(window, expression, timeout = 12000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await window.webContents.executeJavaScript(`Boolean(${expression})`)) return;
    await new Promise(resolve => setTimeout(resolve, 80));
  }
  throw new Error(`Timed out waiting for ${expression}`);
}

async function captureEvidence(window, filename) {
  process.stdout.write(`Capturing ${filename}\n`);
  // Hidden Electron windows can expose the newly committed DOM one compositor
  // frame before capturePage sees it. Discard one capture after two paints so
  // every evidence file represents the requested workspace, not the prior one.
  await window.webContents.executeJavaScript("new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))");
  await window.capturePage();
  await new Promise(resolve => setTimeout(resolve, 160));
  fs.writeFileSync(path.join(EVIDENCE_DIR, filename), (await window.capturePage()).toPNG());
}

async function verifyImageViewport(window, label) {
  await window.webContents.executeJavaScript(`document.querySelector('[aria-label="图片生成"]')?.click()`);
  const result = await window.webContents.executeJavaScript(`(async()=>{
    await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
    const field=document.querySelector('input[aria-label="LoRA 权重"]');
    if(!field)return {ok:false,error:'LoRA weight field missing'};
    field.scrollIntoView({block:'nearest'});field.focus({preventScroll:true});
    const fr=field.getBoundingClientRect(),sc=document.querySelector('.image-generator-scroll').getBoundingClientRect();
    const stage=document.querySelector('.image-stage').getBoundingClientRect();
    const action=document.querySelector('.image-generator footer button').getBoundingClientRect();
    return {width:innerWidth,height:innerHeight,canvasWidth:stage.width,canvasHeight:stage.height,
      weightHeight:fr.height,keyboardFocus:document.activeElement===field,
      ok:fr.top>=sc.top&&fr.bottom<=sc.bottom&&fr.height>=28&&stage.width>200&&stage.height>150&&
        action.bottom<=innerHeight&&action.right<=innerWidth&&action.height>=28&&
        document.documentElement.scrollWidth===document.documentElement.clientWidth&&
        document.documentElement.scrollHeight===document.documentElement.clientHeight};
  })()`);
  if(!result.ok)throw new Error(`image viewport ${label}: ${JSON.stringify(result)}`);
  await captureEvidence(window, `image-workspace-${label}.png`);
  return {label,...result};
}

async function verifyConfigurationViewport(window, label) {
  await window.webContents.executeJavaScript(`document.querySelector('[aria-label="模型管理"]').click()`);
  await window.webContents.executeJavaScript(`(()=>{
    const row=[...document.querySelectorAll('.model-row')].find(node=>node.textContent.includes('Pony V6 XL'));
    if(!row)throw new Error('imported model not listed');
    row.querySelector('.model-action-settings').click();
  })()`);
  await waitFor(window, "document.querySelector('.configuration-model-fields select')");
  await window.webContents.executeJavaScript(`(()=>{
    const selects=document.querySelectorAll('.configuration-model-fields select');
    selects[2].value='vae-smoke';selects[2].dispatchEvent(new Event('change',{bubbles:true}));
    for(const input of document.querySelectorAll('.configuration-confirmations input'))input.click();
  })()`);
  await window.webContents.executeJavaScript(`([...document.querySelectorAll('.model-settings footer button')].find(node=>node.textContent.includes('检查变更'))).click()`);
  await waitFor(window, "document.querySelector('.apply-summary')?.textContent.includes('需安全重启容器')");
  await new Promise(resolve => setTimeout(resolve, 180));
  const result = await window.webContents.executeJavaScript(`(()=>{
    const panel=document.querySelector('.model-settings'), scroll=panel.querySelector('.settings-scroll');
    const field=panel.querySelector('.configuration-model-fields select');
    scroll.scrollTop=0;field.focus({preventScroll:true});
    const footer=panel.querySelector('footer'),pr=panel.getBoundingClientRect(),fr=footer.getBoundingClientRect();
    const disabledOption=panel.querySelector('option[value="vae-quarantined"]');
    const fields=panel.querySelector('.settings-controls');
    return {label:${JSON.stringify(label)},width:innerWidth,height:innerHeight,
      panelWidth:pr.width,scrolls:scroll.scrollHeight>scroll.clientHeight,keyboardFocus:document.activeElement===field,
      formBorder:getComputedStyle(fields).borderTopWidth,disabledVae:Boolean(disabledOption?.disabled),
      panel:{top:pr.top,right:pr.right,bottom:pr.bottom},footer:{left:fr.left,right:fr.right,bottom:fr.bottom},
      buttons:[...footer.querySelectorAll('button')].map(button=>({text:button.textContent,height:button.getBoundingClientRect().height})),
      ok:fr.bottom<=innerHeight+1/devicePixelRatio&&fr.right<=innerWidth+1/devicePixelRatio&&fr.left>=0&&pr.top>=0&&
        Boolean(disabledOption?.disabled)&&getComputedStyle(fields).borderTopWidth==='0px'&&
        [...footer.querySelectorAll('button')].every(button=>button.getBoundingClientRect().height>=28)&&
        document.documentElement.scrollWidth===document.documentElement.clientWidth&&
        document.documentElement.scrollHeight===document.documentElement.clientHeight};
  })()`);
  if (!result.ok) throw new Error('configuration viewport: '+JSON.stringify(result));
  window.webContents.debugger.attach('1.3');
  await window.webContents.debugger.sendCommand('Input.dispatchKeyEvent', {type:'keyDown',key:'Tab',code:'Tab',windowsVirtualKeyCode:9});
  await window.webContents.debugger.sendCommand('Input.dispatchKeyEvent', {type:'keyUp',key:'Tab',code:'Tab',windowsVirtualKeyCode:9});
  window.webContents.debugger.detach();
  await new Promise(resolve=>setTimeout(resolve,80));
  const keyboard = await window.webContents.executeJavaScript(`document.activeElement===document.querySelectorAll('.configuration-model-fields select')[1]`);
  if(!keyboard)throw new Error('configuration editor keyboard tab order changed');
  await captureEvidence(window, `configuration-editor-${label}.png`);
  await window.webContents.executeJavaScript(`document.querySelector('.model-settings .settings-scroll').scrollTop=10000`);
  await captureEvidence(window, `configuration-preview-${label}.png`);
  await window.webContents.executeJavaScript(`document.querySelector('.model-settings-close').click()`);
  await new Promise(resolve=>setTimeout(resolve,180));
  return result;
}

async function verifyTimeoutFeedback(window) {
  fs.mkdirSync(EVIDENCE_DIR, { recursive: true });
  const timeoutChecks = [];
  await window.webContents.executeJavaScript(`document.querySelector('[aria-label="后台任务"][aria-expanded="true"]')?.click()`);
  for (const [label,width,height,factor] of [['desktop',1440,900,1],['compact',1050,680,1],['125',1440,900,1.25],['150',1440,900,1.5]]) {
    window.setSize(width,height); window.webContents.setZoomFactor(factor);
    for (const service of ['image','video']) {
      for (const state of ['running','stopping','unconfirmed','confirmed','oom-unconfirmed','oom-confirmed','reset-unconfirmed','reset-confirmed']) {
        await window.webContents.executeJavaScript(`window.mediaCenterDesktop.request('/__smoke__/timeout/${service}/${state}')`);
        // Opening through the task list exercises the same navigation as a notification.
        await window.webContents.executeJavaScript(`document.querySelector('[aria-label="后台任务"][aria-expanded="false"]')?.click()`);
        const expected = {running:'执行中',stopping:'超时 · 正在停止',unconfirmed:'执行超时',confirmed:'执行已停止',
          'oom-unconfirmed':'运行内存不足','oom-confirmed':'执行已停止','reset-unconfirmed':'模型清理失败','reset-confirmed':'执行已停止'}[state];
        await waitFor(window, `([...document.querySelectorAll('button.status-row')].find(row=>row.textContent.includes('庭院中的柔和蓝色灯光')))?.textContent.includes(${JSON.stringify(expected)})`);
        await window.webContents.executeJavaScript(`([...document.querySelectorAll('button.status-row')].find(row=>row.textContent.includes('庭院中的柔和蓝色灯光'))).click()`);
        const selector = service==='image'?'.image-task-overlay':'.task-monitor-body';
        const panelExpected = state==='reset-confirmed'&&service==='image'?'原执行环境已退出':expected;
        await waitFor(window, `document.querySelector('${selector}')?.textContent.includes(${JSON.stringify(panelExpected)})`);
        const check = await window.webContents.executeJavaScript(`(()=>{
          const panel=document.querySelector('${selector}'),bounds=panel.getBoundingClientRect();
          const buttons=[...panel.querySelectorAll('button')];
          const action=buttons.find(button=>${JSON.stringify(['running','stopping'].includes(state))}?button.title.includes('停止')||button.title==='取消任务':button.title.includes('重试'));
          action.focus();
          const rect=action.getBoundingClientRect();
          return {label:${JSON.stringify(label)},service:${JSON.stringify(service)},state:${JSON.stringify(state)},
            text:panel.textContent.trim(),disabled:action.disabled,keyboardFocus:document.activeElement===action,
            viewport:{width:innerWidth,height:innerHeight},action:rect.toJSON(),panel:bounds.toJSON(),
            ok:action.disabled===${JSON.stringify(['stopping','unconfirmed','oom-unconfirmed','reset-unconfirmed'].includes(state))}&&
              (action.disabled||document.activeElement===action)&&rect.height>=28&&rect.width>=28&&
              rect.left>=0&&rect.right<=innerWidth&&rect.top>=0&&rect.bottom<=innerHeight&&
              bounds.left>=-1&&bounds.right<=innerWidth+1&&!panel.textContent.includes('task_timed_out')&&
              !panel.textContent.includes('timeout_stopping')&&
              !panel.textContent.includes('model_out_of_memory')&&!panel.textContent.includes('adapter_reset_failed')&&
              buttons.every((button,index)=>buttons.slice(index+1).every(other=>{const a=button.getBoundingClientRect(),b=other.getBoundingClientRect();return a.right<=b.left||b.right<=a.left||a.bottom<=b.top||b.bottom<=a.top;}))};
        })()`);
        if(!check.ok) { await captureEvidence(window, `timeout-failed-${service}-${state}-${label}.png`); throw new Error('timeout UI feedback invalid: '+JSON.stringify(check)); }
        timeoutChecks.push(check);
        if(['stopping','confirmed','oom-unconfirmed','oom-confirmed','reset-unconfirmed','reset-confirmed'].includes(state))
          await captureEvidence(window, `recovery-${service}-${state}-${label}.png`);
      }
    }
  }
  window.webContents.setZoomFactor(1);
  if(errors.length)throw new Error(errors.join('\n'));
  return timeoutChecks;
}

async function run() {
  const deadline = setTimeout(() => { process.stderr.write('Electron smoke deadline exceeded\n'); app.exit(1); }, 180000);
  await app.whenReady();
  protocol.handle("mediacenter-smoke", request => {
    try {
      const target = rendererPath(request.url);
      return fs.statSync(target).isFile() ? net.fetch(pathToFileURL(target).toString()) : new Response("Not Found", { status: 404 });
    } catch { return new Response("Not Found", { status: 404 }); }
  });
  const window = new BrowserWindow({ show: false, width: 1440, height: 900, webPreferences: { backgroundThrottling: false, contextIsolation: true, nodeIntegration: false, sandbox: true, preload: path.join(__dirname, "electron-smoke-preload.js") } });
  window.webContents.on("console-message", details => { const message = details?.message ?? ""; if (/error|uncaught|failed/i.test(message)) errors.push(message); });
  window.webContents.on("render-process-gone", (_event, details) => errors.push(`renderer gone: ${details.reason}`));
  await window.loadURL(APP_URL);
  // A prior failed zoom check must not change the next run's desktop baseline.
  window.webContents.setZoomFactor(1);
  await waitFor(window, "document.querySelector('.desktop-shell')");
  if (process.env.MEDIACENTER_SMOKE_CONFIRMATION_ONLY === '1') {
    fs.mkdirSync(EVIDENCE_DIR, { recursive: true });
    if (process.env.MEDIACENTER_SMOKE_COMPACT === '1') window.setSize(1050, 680);
    window.webContents.setZoomFactor(Number(process.env.MEDIACENTER_SMOKE_ZOOM || 1));
    await verifyConfirmationLayer();
    if (errors.length) throw new Error(errors.join('\n'));
    process.stdout.write(JSON.stringify({confirmationLayer:true,evidence:EVIDENCE_DIR})+'\n');
    clearTimeout(deadline); window.destroy(); app.quit(); return;
  }
  if (process.env.MEDIACENTER_SMOKE_TIMEOUT_ONLY === '1') {
    const timeoutChecks = await verifyTimeoutFeedback(window);
    process.stdout.write(JSON.stringify({timeoutChecks,evidence:EVIDENCE_DIR})+'\n');
    clearTimeout(deadline); window.destroy(); app.quit(); return;
  }
  const result = await window.webContents.executeJavaScript(`(async()=>{
    const labels=['运行总览','图片生成','视频生成','语音生成','音乐合成','服务配置','模型管理','服务器硬件','审计日志'];
    const buttons=[...document.querySelectorAll('.sidebar-item')];
    const missing=labels.filter(label=>!buttons.some(button=>button.getAttribute('aria-label')===label));
    if(missing.length)return {ok:false,error:'missing navigation: '+missing.join(',')};
    const typographyErrors=[];
    const targetErrors=[];
    const inspectTypography=(scope,label)=>{
      const controls=new Set(['BUTTON','INPUT','SELECT','TEXTAREA']);
      for(const node of scope.querySelectorAll('*')){
        const style=getComputedStyle(node);
        const rect=node.getBoundingClientRect();
        const directText=[...node.childNodes].some(child=>child.nodeType===Node.TEXT_NODE&&child.textContent.trim());
        if(!directText&&!controls.has(node.tagName))continue;
        if(style.display==='none'||style.visibility==='hidden'||Number(style.opacity)===0||rect.width===0||rect.height===0)continue;
        const size=Number.parseFloat(style.fontSize);
        if(size<10)typographyErrors.push(label+': '+node.tagName.toLowerCase()+'.'+String(node.className||'').replace(/\\s+/g,'.')+' = '+size+'px');
      }
    };
    const inspectTargets=(scope,label)=>{
      for(const node of scope.querySelectorAll('button')){
        const style=getComputedStyle(node);const rect=node.getBoundingClientRect();
        if(style.display==='none'||style.visibility==='hidden'||rect.width===0||rect.height===0)continue;
        if(rect.width<28||rect.height<28)targetErrors.push(label+': '+String(node.className||node.getAttribute('aria-label')||node.title||'button')+' = '+Math.round(rect.width)+'x'+Math.round(rect.height));
      }
    };
    const stage=document.querySelector('.image-stage');stage.dataset.identity='stable';
    for(const label of labels){buttons.find(button=>button.getAttribute('aria-label')===label).click();await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));const visible=[...document.querySelectorAll('.workbench-view')].filter(node=>getComputedStyle(node).display!=='none');if(visible.length!==1)return {ok:false,error:label+' visible views '+visible.length};inspectTypography(visible[0],label);inspectTargets(visible[0],label);}
    inspectTypography(document.querySelector('.desktop-titlebar'),'桌面标题栏');
    inspectTypography(document.querySelector('.desktop-statusbar'),'桌面状态栏');
    if(typographyErrors.length)return {ok:false,error:'undersized visible text: '+typographyErrors.slice(0,8).join(' | ')};
    if(targetErrors.length)return {ok:false,error:'undersized pointer target: '+targetErrors.slice(0,8).join(' | ')};
    buttons.find(button=>button.getAttribute('aria-label')==='运行总览').click();await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
    const overviewConsole=document.querySelector('.overview-console');
    const overviewLayout=overviewConsole?getComputedStyle(overviewConsole).gridTemplateColumns:'';
    if(!overviewConsole||!document.querySelector('.overview-command')||!document.querySelector('.overview-launchbar')||!document.querySelector('.overview-work-state')||document.querySelector('.metric-strip,.dashboard-grid,.launcher-surface'))return {ok:false,error:'overview desktop console contract failed: '+overviewLayout};
    const firstOverviewTask=document.querySelector('.overview-task-list .task-line');
    const activityHeader=document.querySelector('.overview-activity-pane > .overview-pane-head');
    const taskTopGap=firstOverviewTask&&activityHeader?Math.round(firstOverviewTask.getBoundingClientRect().top-activityHeader.getBoundingClientRect().bottom):0;
    const overviewTaskHeight=firstOverviewTask?Math.round(firstOverviewTask.getBoundingClientRect().height):0;
    if(firstOverviewTask&&(Math.abs(taskTopGap)>2||overviewTaskHeight>42))return {ok:false,error:'overview task stream density failed: '+JSON.stringify({taskTopGap,overviewTaskHeight})};
    buttons.find(button=>button.getAttribute('aria-label')==='服务配置').click();await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
    const serviceConfigRows=[...document.querySelectorAll('.service-config-row')];
    const serviceConfigHeight=serviceConfigRows[0]?.getBoundingClientRect().height||0;
    const serviceConfigList=document.querySelector('.service-config-list');
    if(!serviceConfigList||serviceConfigRows.length<4||serviceConfigHeight>72||document.querySelector('.service-config-card,.service-config-grid'))return {ok:false,error:'service continuous-list contract failed: '+JSON.stringify({rows:serviceConfigRows.length,serviceConfigHeight})};
    buttons.find(button=>button.getAttribute('aria-label')==='图片生成').click();await new Promise(resolve=>requestAnimationFrame(resolve));
    if(document.querySelector('.image-stage')?.dataset.identity!=='stable')return {ok:false,error:'image canvas node replaced'};
    const historyScroller=document.querySelector('.image-history > div');
    const historyPaddingBottom=Number.parseFloat(getComputedStyle(historyScroller).paddingBottom);
    const historyBandHeight=document.querySelector('.image-history')?.getBoundingClientRect().height||0;
    const historyCardHeight=document.querySelector('.image-history article')?.getBoundingClientRect().height||0;
    if(historyPaddingBottom<10||historyBandHeight<106||historyBandHeight>114||!historyCardHeight||historyCardHeight<62||historyCardHeight>66)return {ok:false,error:'image history density contract failed: '+JSON.stringify({historyPaddingBottom,historyBandHeight,historyCardHeight})};
    const modelPicker=document.querySelector('.image-model-picker');
    if(!modelPicker)return {ok:false,error:'image model picker missing'};
    modelPicker.open=true;await new Promise(resolve=>requestAnimationFrame(resolve));
    const modelOption=modelPicker.querySelector('[role="option"]');
    const modelRowHeight=modelOption?.getBoundingClientRect().height||0;
    const semanticHeaderIcons=document.querySelectorAll('.image-generator-scroll > section > header .app-icon').length;
    modelPicker.open=false;
    if(!modelOption||modelRowHeight>32||semanticHeaderIcons<3)return {ok:false,error:'image model picker density/icon contract failed: '+JSON.stringify({modelRowHeight,semanticHeaderIcons})};
    buttons.find(button=>button.getAttribute('aria-label')==='模型管理').click();await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
    const serviceRows=[...document.querySelectorAll('.model-row')];
    const serviceRowHeight=Math.max(...serviceRows.map(row=>row.getBoundingClientRect().height));
    const actions=['启动容器服务','停止容器服务','配置显存策略'];
    const missingActions=actions.filter(title=>!document.querySelector('[title="'+title+'"]'));
    const blockedRow=serviceRows.find(row=>row.textContent.includes('AI 超分'));
    blockedRow?.click();await new Promise(resolve=>requestAnimationFrame(resolve));
    const blockedState=document.querySelector('.runtime-error')?.textContent||'';
    const runtimeSummary=document.querySelector('.model-runtime-summary');
    if(serviceRows.length<2||serviceRowHeight<46||serviceRowHeight>50||missingActions.length||!runtimeSummary||document.querySelector('.model-status-board')||!blockedState.includes('GPU 容量不足')||!blockedState.includes('任务会继续排队'))return {ok:false,error:'model lifecycle density/state contract failed: '+JSON.stringify({rows:serviceRows.length,serviceRowHeight,missingActions,runtimeSummary:Boolean(runtimeSummary),blockedState})};
    if(document.querySelector('.model-settings'))return {ok:false,error:'model settings inspector is open by default'};
    const modelBody=document.querySelector('.model-body');
    const closedColumns=getComputedStyle(modelBody).gridTemplateColumns;
    blockedRow?.querySelector('.model-action-settings')?.click();await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
    const inspector=document.querySelector('.model-settings');
    if(inspector)inspectTargets(inspector,'模型设置');
    const openColumns=getComputedStyle(modelBody).gridTemplateColumns;
    const disabledResidency=[...document.querySelectorAll('.residency-choice.disabled')];
    const disabledStyle=disabledResidency[0]?getComputedStyle(disabledResidency[0]):null;
    const disabledColor=disabledStyle?.color||'';
    const enabledColor=getComputedStyle(document.querySelector('.residency-choice:not(.disabled)')).color;
    if(!inspector||targetErrors.length||!document.querySelector('.model-settings-close')||closedColumns===openColumns||disabledResidency.length!==2||disabledColor===enabledColor||!disabledResidency.every(row=>row.querySelector('input')?.disabled))return {ok:false,error:'model settings inspector contract failed: '+JSON.stringify({closedColumns,openColumns,inspector:Boolean(inspector),targetErrors,disabledResidency:disabledResidency.length,disabledColor,enabledColor})};
    document.querySelector('.model-settings-close').click();await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
    const closingInspector=document.querySelector('.model-settings');
    if(closingInspector&&!closingInspector.classList.contains('inspector-leave-active'))return {ok:false,error:'model settings inspector did not enter close transition: '+closingInspector.className};
    await new Promise(resolve=>setTimeout(resolve,140));
    const importEntry=[...document.querySelectorAll('.model-commandbar button')].find(node=>node.textContent.includes('导入并部署'));
    importEntry?.click();await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
    const importWizard=document.querySelector('.model-import-wizard');
    const importSteps=document.querySelectorAll('.import-steps button').length;
    const sourceChoices=document.querySelectorAll('.source-options > button').length;
    if(importWizard){inspectTypography(importWizard,'模型导入向导');inspectTargets(importWizard,'模型导入向导');}
    if(!importWizard||importSteps!==5||sourceChoices!==2||targetErrors.length||typographyErrors.length)return {ok:false,error:'model import wizard contract failed: '+JSON.stringify({wizard:Boolean(importWizard),importSteps,sourceChoices,targetErrors:targetErrors.slice(-4),typographyErrors:typographyErrors.slice(-4)})};
    document.querySelector('[aria-label="关闭导入向导"]')?.click();await new Promise(resolve=>setTimeout(resolve,140));
    const messages=document.querySelector('[aria-label="消息"]');messages.click();await new Promise(resolve=>requestAnimationFrame(resolve));const statusCenter=document.querySelector('.status-center-panel');if(!statusCenter)return {ok:false,error:'status center did not open'};inspectTypography(statusCenter,'状态中心');
    if(typographyErrors.length)return {ok:false,error:'undersized visible text: '+typographyErrors.slice(0,8).join(' | ')};
    const rootStyle=getComputedStyle(document.documentElement);
    const theme={canvas:rootStyle.getPropertyValue('--mw-canvas').trim(),accent:rootStyle.getPropertyValue('--mw-accent').trim(),controlRadius:rootStyle.getPropertyValue('--mw-radius-control').trim(),panelRadius:rootStyle.getPropertyValue('--mw-radius-panel').trim(),dialogRadius:rootStyle.getPropertyValue('--mw-radius-dialog').trim(),titleHeight:rootStyle.getPropertyValue('--chrome-title').trim(),sidebarWidth:rootStyle.getPropertyValue('--chrome-sidebar').trim(),statusHeight:rootStyle.getPropertyValue('--chrome-status').trim()};
    if(theme.canvas!=='#090b10'||theme.accent!=='#49cddd'||theme.controlRadius!=='5px'||theme.panelRadius!=='6px'||theme.dialogRadius!=='10px'||theme.titleHeight!=='38px'||theme.sidebarWidth!=='48px'||theme.statusHeight!=='26px')return {ok:false,error:'unexpected theme tokens '+JSON.stringify(theme)};
    return {ok:true,nav:labels.length,views:document.querySelectorAll('.workbench-view').length,title:document.title,minVisibleFontPx:10,overview:{layout:overviewLayout,taskTopGap,taskHeight:overviewTaskHeight},services:{rows:serviceConfigRows.length,rowHeight:serviceConfigHeight},imageLayout:{historyPaddingBottom,historyBandHeight,historyCardHeight,modelRowHeight,semanticHeaderIcons},modelLifecycle:{serviceRowHeight,runtimeSummary:Boolean(runtimeSummary),blockedState,closedColumns,openColumns,disabledResidency:disabledResidency.length,disabledColor,enabledColor},modelImport:{steps:importSteps,sourceChoices},theme};
  })()`);
  if (!result.ok) throw new Error(result.error);
  fs.mkdirSync(EVIDENCE_DIR, { recursive: true });
  await window.webContents.executeJavaScript(`document.querySelector('[aria-label="消息"][aria-expanded="true"]')?.click()`);
  await window.webContents.executeJavaScript(`document.querySelector('[aria-label="后台任务"]')?.click()`);
  await waitFor(window, "document.querySelectorAll('.deployment-status-row').length === 2");
  const deploymentFeedback = await window.webContents.executeJavaScript(`(()=>{
    const rows=[...document.querySelectorAll('.deployment-status-row')];
    const panel=document.querySelector('.status-center-panel');
    const buttons=rows.flatMap(row=>[...row.querySelectorAll('button')]);
    return {ok:rows[0].textContent.includes('准备 Runtime')&&rows[1].textContent.includes('运行环境传输中断')&&
      buttons.length===2&&buttons.every(button=>button.getBoundingClientRect().height>=28)&&
      panel.getBoundingClientRect().bottom<=innerHeight,states:rows.map(row=>row.textContent.trim())};
  })()`);
  if(!deploymentFeedback.ok)throw new Error('deployment status recovery feedback invalid: '+JSON.stringify(deploymentFeedback));
  await captureEvidence(window, 'deployment-status-recovery.png');
  await window.webContents.executeJavaScript(`document.querySelector('[aria-label="后台任务"][aria-expanded="true"]')?.click()`);
  await window.webContents.executeJavaScript(`document.querySelector('[aria-label="运行总览"]')?.click()`);
  await captureEvidence(window, "overview.png");
  await window.webContents.executeJavaScript(`document.querySelector('[aria-label="模型管理"]')?.click()`);
  await window.webContents.executeJavaScript(`(()=>{const row=[...document.querySelectorAll('.model-row')].find(item=>item.textContent.includes('AI 超分'));row?.querySelector('.model-action-settings')?.click();})()`);
  await new Promise(resolve => setTimeout(resolve, 180));
  await captureEvidence(window, "model-center.png");
  await window.webContents.executeJavaScript(`document.querySelector('.model-settings-close')?.click()`);
  await new Promise(resolve => setTimeout(resolve, 140));
  await window.webContents.executeJavaScript(`([...document.querySelectorAll('.model-commandbar button')].find(node=>node.textContent.includes('导入并部署')))?.click()`);
  await new Promise(resolve => setTimeout(resolve, 140));
  await captureEvidence(window, "model-import-wizard.png");
  await window.webContents.executeJavaScript(`document.querySelector('[aria-label="关闭导入向导"]')?.click()`);
  await window.webContents.executeJavaScript(`document.querySelector('[aria-label="图片生成"]')?.click()`);
  await window.webContents.executeJavaScript(`(()=>{
    const select=document.querySelector('select[aria-label="任务 LoRA"]');
    if(!select||select.options.length!==2)throw new Error('LoRA selector must contain only verified compatible assets');
    select.value='lora-shadow@v1';select.dispatchEvent(new Event('change',{bubbles:true}));
    const details=document.querySelector('.lora-unavailable');if(details)details.open=true;
  })()`);
  await captureEvidence(window, "image-workspace.png");
  await window.webContents.executeJavaScript(`document.querySelector('[aria-label="消息"]')?.click()`);
  await captureEvidence(window, "status-center.png");
  await window.webContents.executeJavaScript(`document.querySelector('[aria-label="消息"][aria-expanded="true"]')?.click()`);
  await window.webContents.executeJavaScript(`document.querySelector('[aria-label="服务配置"]')?.click()`);
  await captureEvidence(window, "services.png");
  await window.webContents.executeJavaScript(`document.querySelector('[aria-label="服务器硬件"]')?.click()`);
  await captureEvidence(window, "hardware.png");
  await window.webContents.executeJavaScript(`document.querySelector('[aria-label="视频生成"]')?.click()`);
  await captureEvidence(window, "video-workspace.png");
  await window.webContents.executeJavaScript(`document.querySelector('[aria-label="语音生成"]')?.click()`);
  await captureEvidence(window, "speech-workspace.png");
  const configurationViewportChecks = [await verifyConfigurationViewport(window, 'desktop')];
  window.setSize(1050, 680);
  await waitFor(window, "window.innerWidth <= 1050 && window.innerHeight <= 680");
  const compact = await window.webContents.executeJavaScript(`(()=>{const shell=document.querySelector('.desktop-shell').getBoundingClientRect();const workbench=document.querySelector('.workbench').getBoundingClientRect();return {ok:document.documentElement.scrollWidth===document.documentElement.clientWidth&&document.documentElement.scrollHeight===document.documentElement.clientHeight&&shell.width>0&&shell.height>0&&workbench.width>700&&workbench.height>500,width:window.innerWidth,height:window.innerHeight};})()`);
  if (!compact.ok) throw new Error(`compact viewport overflow: ${JSON.stringify(compact)}`);
  const imageViewportChecks = [await verifyImageViewport(window, 'compact')];
  configurationViewportChecks.push(await verifyConfigurationViewport(window, 'compact'));
  const zoomResults = [];
  window.setSize(1440, 900);
  for (const factor of [1.25, 1.5]) {
    window.webContents.setZoomFactor(factor);
    await new Promise(resolve => setTimeout(resolve, 180));
    await window.webContents.executeJavaScript(`document.querySelector('[aria-label="模型管理"]')?.click()`);
    await window.webContents.executeJavaScript(`([...document.querySelectorAll('.model-commandbar button')].find(node=>node.textContent.includes('导入并部署')))?.click()`);
    await new Promise(resolve => setTimeout(resolve, 180));
    const zoom = await window.webContents.executeJavaScript(`(()=>{const wizard=document.querySelector('.model-import-wizard');const footer=document.querySelector('.import-footer');const action=[...footer.querySelectorAll('button')].at(-1);const wr=wizard.getBoundingClientRect();const ar=action.getBoundingClientRect();return {factor:${factor},width:window.innerWidth,height:window.innerHeight,wizard:{left:wr.left,top:wr.top,right:wr.right,bottom:wr.bottom},action:{left:ar.left,top:ar.top,right:ar.right,bottom:ar.bottom},ok:wr.left>=0&&wr.top>=0&&wr.right<=window.innerWidth&&wr.bottom<=window.innerHeight&&ar.left>=wr.left&&ar.right<=wr.right&&ar.top>=wr.top&&ar.bottom<=wr.bottom&&document.documentElement.scrollWidth===document.documentElement.clientWidth&&document.documentElement.scrollHeight===document.documentElement.clientHeight};})()`);
    if (!zoom.ok) throw new Error(`model import zoom overflow: ${JSON.stringify(zoom)}`);
    zoomResults.push(zoom);
    await captureEvidence(window, `model-import-${Math.round(factor * 100)}.png`);
    await window.webContents.executeJavaScript(`document.querySelector('[aria-label="关闭导入向导"]')?.click()`);
    await new Promise(resolve => setTimeout(resolve, 140));
  }
  for (const factor of [1.25, 1.5]) {
    window.webContents.setZoomFactor(factor);
    await new Promise(resolve=>setTimeout(resolve,180));
    imageViewportChecks.push(await verifyImageViewport(window, String(factor*100)));
    configurationViewportChecks.push(await verifyConfigurationViewport(window, String(factor*100)));
  }
  window.webContents.setZoomFactor(1);
  if (errors.length) throw new Error(errors.join("\n"));
  await window.webContents.executeJavaScript(`document.querySelector('[aria-label="模型管理"]')?.click()`);
  await verifyConfirmationLayer();
  async function verifyConfirmationLayer() {
  await window.webContents.executeJavaScript(`document.querySelector('[aria-label="模型管理"]')?.click()`);
  await window.webContents.executeJavaScript(`([...document.querySelectorAll('.model-commandbar button')].find(n=>n.textContent.includes('安装中心'))).click()`);
  await waitFor(window, "document.querySelector('.install-center')");
  await window.webContents.executeJavaScript(`([...document.querySelectorAll('.install-body aside button')].find(n=>n.textContent.includes('Pony V6 XL'))).click()`);
  await waitFor(window, "[...document.querySelectorAll('.install-center button')].some(n=>n.textContent.includes('卸载服务')&&!n.disabled)");
  await window.webContents.executeJavaScript(`(()=>{const b=[...document.querySelectorAll('.install-center button')].find(n=>n.textContent.includes('卸载服务'));b.focus();b.click()})()`);
  await waitFor(window, "document.querySelector('dialog:modal')");
  const confirmationLayer = await window.webContents.executeJavaScript(`(()=>{
    const panel=document.querySelector('.confirm-panel'),r=panel.getBoundingClientRect();
    return {topmost:panel.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2)),
      cancelFocused:document.activeElement?.textContent==='返回',
      contained:r.left>=0&&r.top>=0&&r.right<=innerWidth&&r.bottom<=innerHeight,
      targets:[...panel.querySelectorAll('button')].every(b=>b.getBoundingClientRect().height>=28)};
  })()`);
  if(Object.values(confirmationLayer).some(v=>!v))throw new Error('confirmation layer: '+JSON.stringify(confirmationLayer));
  await captureEvidence(window, 'confirmation-over-install-center.png');
  window.webContents.debugger.attach('1.3');
  for(const stroke of ['Tab','Tab','Tab','Shift+Tab','Escape']) {
    const key=stroke==='Shift+Tab'?'Tab':stroke,modifiers=stroke==='Shift+Tab'?8:0;
    await window.webContents.debugger.sendCommand('Input.dispatchKeyEvent',{type:'keyDown',key,code:key,modifiers,windowsVirtualKeyCode:key==='Tab'?9:27});
    await window.webContents.debugger.sendCommand('Input.dispatchKeyEvent',{type:'keyUp',key,code:key,modifiers,windowsVirtualKeyCode:key==='Tab'?9:27});
    if(key==='Tab') {
      const focus=await window.webContents.executeJavaScript(`({inside:Boolean(document.activeElement?.closest('.confirmation-dialog')),tag:document.activeElement?.tagName,text:document.activeElement?.textContent?.slice(0,100),modal:Boolean(document.querySelector('dialog:modal'))})`);
      if(!focus.inside)throw new Error('modal focus escaped: '+JSON.stringify(focus));
    }
  }
  window.webContents.debugger.detach();
  await waitFor(window, "!document.querySelector('dialog:modal')");
  if(!await window.webContents.executeJavaScript(`document.activeElement?.textContent.includes('卸载服务')`))throw new Error('trigger focus not restored');
  await window.webContents.executeJavaScript(`document.querySelector('.install-center > header button').click()`);
  }
  const removalChecks = [];
  await window.webContents.executeJavaScript(`document.querySelector('[aria-label="模型管理"]')?.click()`);
  for (const state of ['accepted','failed','ready']) {
    await window.webContents.executeJavaScript(`window.mediaCenterDesktop.request('/__smoke__/removal/${state}')`);
    const expected = {accepted:'正在卸载',failed:'卸载未完成',ready:'已卸载 · 资产保留'}[state];
    if (state === 'accepted') await window.webContents.executeJavaScript(`document.querySelector('[aria-label="后台任务"]')?.click()`);
    await waitFor(window, `document.body.textContent.includes(${JSON.stringify(expected)})`);
    const check = await window.webContents.executeJavaScript(`(()=>{
      const model=[...document.querySelectorAll('.model-row')].find(row=>row.textContent.includes('Pony V6 XL'));
      const status=[...document.querySelectorAll('.deployment-status-row')].find(row=>row.textContent.includes('服务卸载')&&
        (${JSON.stringify(state)}!=='ready'||row.textContent.includes('dop-smoke-removal-retry')));
      const buttons=[...status.querySelectorAll('button')];
      return {state:${JSON.stringify(state)},text:status.textContent.trim(),
        ok:${JSON.stringify(state)}==='ready'?!model&&buttons.length===0:
          Boolean(model?.querySelector('.model-action-start')?.disabled&&model?.querySelector('.model-action-settings')?.disabled)&&
          !document.querySelector('.model-work-state')?.textContent.includes('正在停止容器')&&
          (${JSON.stringify(state)}==='accepted'?buttons.length===0:buttons.length===1&&!buttons[0].disabled&&buttons[0].textContent.includes('重试卸载'))};
    })()`);
    if(!check.ok)throw new Error('removal UI state invalid: '+JSON.stringify(check));
    removalChecks.push(check);
    await captureEvidence(window, `user-removal-${state}.png`);
  }
  const timeoutChecks = await verifyTimeoutFeedback(window);
  process.stdout.write(JSON.stringify({ ...result, compactViewport: compact, zoomResults, imageViewportChecks, configurationViewportChecks, removalChecks, timeoutChecks, evidence: EVIDENCE_DIR }) + "\n");
  clearTimeout(deadline);
  window.destroy();
  app.quit();
}

run().catch(error => { process.stderr.write(`${error.stack || error}\n`); app.exit(1); });
