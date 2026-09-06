<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, shallowRef, watch } from "vue";
import ActionButton from "@/components/ActionButton.vue";
import AppIcon from "@/components/AppIcon.vue";
import OptionFields from "@/components/OptionFields.vue";
import StateBadge from "@/components/StateBadge.vue";
import { canCancelTask, formatDate, taskElapsed, taskErrorMessage, taskRetryDisabledReason, taskStageLabel, taskStatusLabel } from "@/lib/format";
import { desktopBridge } from "@/services/desktop";
import { imageDraftKey, loraCompatibility, sameModelIdentity, type ImageGenerationDraft } from "@/lib/models";
import { useAppStore } from "@/stores/app";
import type { AssetCompatibility, MediaTask, ResourceItem, ServiceModel } from "@/types/contracts";

type Tool = "select" | "crop" | "mosaic" | "upscale";
interface ImageDocument { key: string; profileId: string; name: string; blob: Blob; url: string; width: number; height: number; dirty: boolean; savedBlob: Blob; undo: Blob[]; redo: Blob[]; taskId?: string }
type Crop = { x: number; y: number; width: number; height: number; ratio: "free" | "1:1" | "4:3" | "16:9" };
type LoraReference = { asset_id: string; revision: string; family: string; weight: number };
type ConfigurationBinding = NonNullable<MediaTask["configuration_binding"]>;
const store = useAppStore();
const service = computed(() => store.services.find(item => item.kind === "image"));
const generationModels = computed(() => (service.value?.models ?? []).filter(item => item.capabilities?.workflow !== "upscale"));
const upscaleModels = computed(() => (service.value?.models ?? []).filter(item => item.capabilities?.workflow === "upscale"));
const selectedModelKey = ref(""); const prompt = ref(""); const options = reactive<Record<string,string|number>>({});
const draftIdentity = ref<Pick<ImageGenerationDraft, 'configuration'|'execution'>>({configuration:null,execution:null});
let loadedDraftKey = "";
const selectedLoraKey = ref(""); const loraWeight = ref(0.8);
const model = computed(() => generationModels.value.find(item => item.model_key === selectedModelKey.value) ?? null);
const modelDeployment = computed<ResourceItem|null>(() => store.deployments.find(item => item.id === selectedModelKey.value) ?? null);
const configuration = computed<ConfigurationBinding|null>(() => (modelDeployment.value?.configuration_binding as ConfigurationBinding | null | undefined) ?? null);
const execution = computed<MediaTask['execution_binding']>(() => modelDeployment.value?.execution_binding ?? null);
const configurationChanged = computed(() => !sameModelIdentity(draftIdentity.value, {configuration:configuration.value,execution:execution.value}));
const vaeLabel = computed(() => store.assets.find(item=>item.id===configuration.value?.vae_asset_id)?.display_name ?? '模型内置 VAE');
const capability = computed<Record<string,any>>(() => model.value?.capabilities ?? { options: [] });
const supportsLora = computed(() => ["sdxl-single-file","sdxl-base-1.0","illustrious-xl-v2.0"].includes(String(model.value?.catalog_key??"")));
function editableField(field:Record<string,any>){return field.minimum === undefined || field.maximum === undefined || field.minimum !== field.maximum;}
const basicFields = computed(() => (capability.value.options ?? []).filter((field:any) => !field.advanced && editableField(field)));
const advancedFields = computed(() => (capability.value.options ?? []).filter((field:any) => field.advanced && editableField(field)));
const ready = computed(() => Boolean(service.value?.enabled && model.value?.healthy));
const imageTasks = computed(() => store.tasks.filter(item => item.service === "image"));
const currentTask = computed(() => store.selectedTask("image"));
const completedTasks = computed(() => imageTasks.value.filter(item => item.status === "succeeded" && item.output?.artifact_url));
const tool = ref<Tool>("select"); const documentState = shallowRef<ImageDocument|null>(null); const historyUrls = reactive<Record<string,string>>({}); const loadedDocuments = new Map<string,ImageDocument>();
const selectedDocumentKeys = new Map<string,string>();
let workspaceMounted = true;
function documentContext(){return {profileId:store.activeProfile?.id??'',epoch:store.connectionEpoch};}
function contextCurrent(context:ReturnType<typeof documentContext>){return workspaceMounted&&context.profileId===(store.activeProfile?.id??'')&&context.epoch===store.connectionEpoch;}
function documentCurrent(doc:ImageDocument,context:ReturnType<typeof documentContext>){return contextCurrent(context)&&doc.profileId===context.profileId&&documentState.value===doc;}
function documentKey(source:string,profileId=store.activeProfile?.id??''){return JSON.stringify([profileId,source]);}
const viewport = ref<HTMLElement|null>(null); const content = ref<HTMLElement|null>(null); const imageNode = ref<HTMLImageElement|null>(null); const mosaicCanvas = ref<HTMLCanvasElement|null>(null);
const zoom = ref(1); const zoomMode = ref<"fit"|"actual"|"custom">("fit"); const pan = reactive({ x: 0, y: 0 }); const crop = reactive<Crop>({ x:0,y:0,width:1,height:1,ratio:"free" }); const mosaic = reactive({ size:36,strength:12,dirty:false,base:null as Blob|null });
const upscaleModelKey = ref(""); let panDrag: { x:number;y:number;px:number;py:number;id:number }|null = null; let cropDrag: any = null; let drawing = false; let resizeObserver: ResizeObserver|null = null;
const dropActive = ref(false);
const transform = computed(() => `translate(-50%, -50%) translate(${pan.x}px, ${pan.y}px) scale(${zoom.value})`);
const cropStyle = computed(() => documentState.value ? ({ left:`${crop.x/documentState.value.width*100}%`, top:`${crop.y/documentState.value.height*100}%`, width:`${crop.width/documentState.value.width*100}%`, height:`${crop.height/documentState.value.height*100}%` }) : {});
const inFlight = computed(() => currentTask.value && ["queued","assigned","running","cancel_requested"].includes(currentTask.value.status));

const loraAssets = computed(() => store.assets.filter(item => item.state === "ready" && item.role === "lora" && item.media_kind === "image"));
const compatibilityLabels: Record<AssetCompatibility["verdict"],string> = {exact:"精确匹配",compatible:"已验证兼容",experimental:"实验性",incompatible:"不兼容",unknown:"未确认"};
function loraKey(asset:ResourceItem){return `${asset.id}@${asset.revision}`;}
function baseFor(key:string){
  const binding=store.deployments.find(item=>item.id===key)?.execution_binding;
  return binding?{asset_id:binding.model_asset_id,revision:binding.model_asset_revision}:null;
}
function compatibilityFor(asset:ResourceItem, key=selectedModelKey.value){
  return loraCompatibility(store.assetCompatibilities, asset, baseFor(key));
}
const loraChoices = computed(() => loraAssets.value.map(asset=>{const evidence=compatibilityFor(asset);return {asset,evidence,allowed:evidence?.verdict==="exact"||evidence?.verdict==="compatible"};}));
const selectedLora = computed(() => loraChoices.value.find(item=>loraKey(item.asset)===selectedLoraKey.value&&item.allowed)??null);
const loraSelectionValid = computed(() => !selectedLoraKey.value || (Boolean(selectedLora.value) &&
  Number.isFinite(loraWeight.value) && loraWeight.value>=-2 && loraWeight.value<=2));

function configurationFor(key:string):ConfigurationBinding|null{return (store.deployments.find(item=>item.id===key)?.configuration_binding as ConfigurationBinding|null|undefined)??null;}
const availableLoras = computed(()=>loraChoices.value.filter(item=>item.allowed));
const unavailableLoras = computed(()=>loraChoices.value.filter(item=>!item.allowed));

function chooseDefault(){ if(!generationModels.value.some(item=>item.model_key===selectedModelKey.value)) selectedModelKey.value=generationModels.value.find(item=>item.model_key===service.value?.default_model&&item.healthy)?.model_key??generationModels.value.find(item=>item.healthy)?.model_key??generationModels.value[0]?.model_key??""; if(!upscaleModels.value.some(item=>item.model_key===upscaleModelKey.value)) upscaleModelKey.value=upscaleModels.value.find(item=>item.healthy)?.model_key??upscaleModels.value[0]?.model_key??""; }
function selectedReference():LoraReference|null {
  if(!selectedLoraKey.value)return null;
  const [asset_id,revision]=selectedLoraKey.value.split('@');
  return {asset_id,revision,family:'sdxl',weight:loraWeight.value};
}
function saveDraft(){
  if(!loadedDraftKey)return;
  store.imageGenerationDrafts[loadedDraftKey]={prompt:prompt.value,options:{...options},
    ...draftIdentity.value,lora:selectedReference()};
}
function loadModel(){
  saveDraft();
  loadedDraftKey=imageDraftKey(store.activeProfile?.id??'',selectedModelKey.value);
  const draft=store.imageGenerationDrafts[loadedDraftKey];
  Object.keys(options).forEach(key=>delete options[key]);
  prompt.value=draft?.prompt??'';
  (model.value?.capabilities?.options??[]).forEach((field:any)=>{options[field.key]=draft?.options[field.key]??field.default});
  draftIdentity.value=JSON.parse(JSON.stringify(draft?{configuration:draft.configuration,execution:draft.execution}:
    {configuration:configuration.value,execution:execution.value}));
  selectedLoraKey.value=draft?.lora?`${draft.lora.asset_id}@${draft.lora.revision}`:'';
  loraWeight.value=draft?.lora?.weight??0.8;
}
function adoptConfiguration(){
  draftIdentity.value=JSON.parse(JSON.stringify({configuration:configuration.value,execution:execution.value}));
  saveDraft();
}
async function ensureLoraCompatibility(){
  const key=selectedModelKey.value,base=baseFor(key),epoch=store.connectionEpoch;
  if(!base||!supportsLora.value)return;
  const missing=loraAssets.value.filter(asset=>!compatibilityFor(asset,key));
  for(let offset=0;offset<missing.length;offset+=2){
    if(epoch!==store.connectionEpoch||key!==selectedModelKey.value)return;
    await Promise.all(missing.slice(offset,offset+2).map(asset=>store.assessAssetCompatibility(asset.id,base.asset_id)));
  }
}
watch(generationModels,chooseDefault,{immediate:true});
watch(()=>imageDraftKey(store.activeProfile?.id??'',selectedModelKey.value),loadModel,{immediate:true});
watch(()=>JSON.stringify([store.connectionEpoch,selectedModelKey.value,execution.value,loraAssets.value.map(item=>[item.id,item.revision])]),()=>void ensureLoraCompatibility(),{immediate:true});
function blobFromBytes(bytes:Uint8Array,type:string){const copy=new Uint8Array(bytes.byteLength);copy.set(bytes);return new Blob([copy.buffer],{type});}
async function dimensions(blob:Blob){const bitmap=await createImageBitmap(blob);const value={width:bitmap.width,height:bitmap.height};bitmap.close();return value;}
function release(doc:ImageDocument){if(doc.url)URL.revokeObjectURL(doc.url);}
async function createDocument(key:string,name:string,blob:Blob,taskId?:string,context=documentContext()){
  if(!contextCurrent(context))return;
  let doc=loadedDocuments.get(key);
  if(!doc){const size=await dimensions(blob);if(!contextCurrent(context))return;doc={key,profileId:context.profileId,name,blob,url:URL.createObjectURL(blob),...size,dirty:false,savedBlob:blob,undo:[],redo:[],taskId};loadedDocuments.set(key,doc);}
  documentState.value=doc;selectedDocumentKeys.set(context.profileId,key);resetToolState();await nextTick();if(contextCurrent(context))fit();return doc;
}
async function selectTask(task:MediaTask){
  const context=documentContext();store.selectedTaskIds.image=task.id;
  if(task.status!=="succeeded"||!task.output?.artifact_url)return;
  const key=documentKey(`task:${task.id}`,context.profileId),existing=loadedDocuments.get(key);
  if(existing){await createDocument(key,existing.name,existing.blob,task.id,context);return;}
  const blob=await store.runAction(`artifact:image:${task.id}`,"正在读取图片",()=>store.fetchArtifactBlob(task.output!.artifact_url!),{record:false});
  if(blob&&contextCurrent(context))await createDocument(key,task.prompt,blob,task.id,context);
}
async function loadCurrentTask(){const task=currentTask.value;if(task?.status==="succeeded"&&task.output?.artifact_url)await selectTask(task);}
watch(()=>currentTask.value?.output?.artifact_url,()=>void loadCurrentTask(),{immediate:true});
async function loadHistory(){const context=documentContext();for(const task of completedTasks.value.slice(0,50)){if(!contextCurrent(context))return;if(historyUrls[task.id])continue;const url=await store.fetchArtifactUrl(task.output!.artifact_url!).catch(()=>"");if(!contextCurrent(context)){if(url)URL.revokeObjectURL(url);return;}if(url){if(historyUrls[task.id])URL.revokeObjectURL(url);else historyUrls[task.id]=url;}}}
watch(()=>completedTasks.value.map(item=>`${item.id}:${item.output?.artifact_url}`).join("|"),()=>void loadHistory(),{immediate:true});
function resetToolState(){tool.value="select";mosaic.dirty=false;mosaic.base=null;if(documentState.value)Object.assign(crop,{x:0,y:0,width:documentState.value.width,height:documentState.value.height,ratio:"free"});}
watch(()=>[store.connectionEpoch,store.activeProfile?.id??''],()=>{
  const previous=documentState.value;
  if(previous){loadedDocuments.set(previous.key,previous);selectedDocumentKeys.set(previous.profileId,previous.key);}
  const selected=selectedDocumentKeys.get(store.activeProfile?.id??'');
  documentState.value=selected?loadedDocuments.get(selected)??null:null;
  Object.entries(historyUrls).forEach(([id,url])=>{URL.revokeObjectURL(url);delete historyUrls[id];});
  panDrag=null;cropDrag=null;drawing=false;dropActive.value=false;resetToolState();
  void nextTick().then(()=>{if(workspaceMounted){fit();void loadHistory();}});
},{flush:'sync'});
async function openLocal(){const context=documentContext();const files=await desktopBridge().pickAssets({kind:"image",multiple:false,accept:["image/"]});const file=files[0];if(!file||!contextCurrent(context))return;const blob=blobFromBytes(file.bytes,file.type);await createDocument(documentKey(`local:${crypto.randomUUID()}`,context.profileId),file.name,blob,undefined,context);}
async function dropLocal(event:DragEvent){dropActive.value=false;const context=documentContext(),file=event.dataTransfer?.files[0];if(!file)return;if(!file.type.startsWith("image/")){store.toast("请拖入图片文件",{type:"error",persistent:true});return;}if(file.size>256*1024*1024){store.toast("图片超过 256 MiB",{type:"error",persistent:true});return;}await createDocument(documentKey(`local:${crypto.randomUUID()}`,context.profileId),file.name,file,undefined,context);}
async function setTool(value:Tool){const context=documentContext();if(tool.value==="mosaic"&&mosaic.dirty&&value!=="mosaic"&&!await store.confirm("放弃马赛克草稿","尚未应用的笔触会丢失。","放弃"))return;if(!contextCurrent(context))return;tool.value=value;if(value==="crop"&&documentState.value)Object.assign(crop,{x:0,y:0,width:documentState.value.width,height:documentState.value.height});if(value==="mosaic")await prepareMosaic();}
async function updateDocumentBlob(doc:ImageDocument,blob:Blob,undo:Blob[],redo:Blob[],context:ReturnType<typeof documentContext>){
  const size=await dimensions(blob);if(!documentCurrent(doc,context))return false;
  URL.revokeObjectURL(doc.url);Object.assign(doc,size,{blob,url:URL.createObjectURL(blob),undo,redo,dirty:blob!==doc.savedBlob});
  documentState.value={...doc};loadedDocuments.set(doc.key,documentState.value);resetToolState();await nextTick();if(contextCurrent(context))fit();return true;
}
async function replaceBlob(blob:Blob,operation:string,doc=documentState.value,context=documentContext()){
  if(!doc||!documentCurrent(doc,context))return;
  if(await updateDocumentBlob(doc,blob,[...doc.undo,doc.blob].slice(-30),[],context))store.addMessage(operation,{detail:`${doc.width}×${doc.height} · 尚未保存到历史`});
}
async function undo(){const doc=documentState.value,context=documentContext();if(!doc?.undo.length||!documentCurrent(doc,context))return;await updateDocumentBlob(doc,doc.undo[doc.undo.length-1],doc.undo.slice(0,-1),[...doc.redo,doc.blob],context);}
async function redo(){const doc=documentState.value,context=documentContext();if(!doc?.redo.length||!documentCurrent(doc,context))return;await updateDocumentBlob(doc,doc.redo[doc.redo.length-1],[...doc.undo,doc.blob],doc.redo.slice(0,-1),context);}
function canvasBlob(canvas:HTMLCanvasElement){return new Promise<Blob>((resolve,reject)=>canvas.toBlob(blob=>blob?resolve(blob):reject(new Error("图片编码失败")),"image/png"));}
async function imageForBlob(blob:Blob){const bitmap=await createImageBitmap(blob);return bitmap;}
async function applyCrop(){const doc=documentState.value,context=documentContext();if(!doc||!documentCurrent(doc,context))return;constrainCrop();const bounds={...crop},bitmap=await imageForBlob(doc.blob);if(!documentCurrent(doc,context)){bitmap.close();return;}const canvas=document.createElement("canvas");canvas.width=bounds.width;canvas.height=bounds.height;canvas.getContext("2d")!.drawImage(bitmap,bounds.x,bounds.y,bounds.width,bounds.height,0,0,bounds.width,bounds.height);bitmap.close();await replaceBlob(await canvasBlob(canvas),"裁剪已应用到当前工作图",doc,context);}
async function prepareMosaic(){const doc=documentState.value,context=documentContext(),canvas=mosaicCanvas.value;if(!doc||!canvas)return;const bitmap=await imageForBlob(doc.blob);if(!documentCurrent(doc,context)||tool.value!=='mosaic'){bitmap.close();return;}canvas.width=doc.width;canvas.height=doc.height;canvas.getContext("2d",{willReadFrequently:true})!.drawImage(bitmap,0,0);bitmap.close();mosaic.base=doc.blob;mosaic.dirty=false;}
function mosaicPoint(event:PointerEvent){const canvas=mosaicCanvas.value!,rect=canvas.getBoundingClientRect();return{x:(event.clientX-rect.left)*canvas.width/rect.width,y:(event.clientY-rect.top)*canvas.height/rect.height};}
function paint(event:PointerEvent){const canvas=mosaicCanvas.value!,point=mosaicPoint(event),ctx=canvas.getContext("2d")!,radius=mosaic.size/2,block=mosaic.strength,x=Math.max(0,Math.round(point.x-radius)),y=Math.max(0,Math.round(point.y-radius)),size=Math.min(Math.round(radius*2),canvas.width-x,canvas.height-y);if(size<1)return;const temp=document.createElement("canvas");temp.width=Math.max(1,Math.round(size/block));temp.height=temp.width;temp.getContext("2d")!.drawImage(canvas,x,y,size,size,0,0,temp.width,temp.height);ctx.save();ctx.beginPath();ctx.arc(point.x,point.y,radius,0,Math.PI*2);ctx.clip();ctx.imageSmoothingEnabled=false;ctx.drawImage(temp,0,0,temp.width,temp.height,x,y,size,size);ctx.restore();mosaic.dirty=true;}
function mosaicDown(event:PointerEvent){if(event.button!==0)return;drawing=true;mosaicCanvas.value?.setPointerCapture(event.pointerId);paint(event);}function mosaicMove(event:PointerEvent){if(drawing)paint(event);}function mosaicUp(){drawing=false;}
async function applyMosaic(){const doc=documentState.value,context=documentContext();if(mosaicCanvas.value&&mosaic.dirty)await replaceBlob(await canvasBlob(mosaicCanvas.value),"马赛克已应用到当前工作图",doc,context);}
async function discardTool(){if(tool.value==="mosaic")await prepareMosaic();if(tool.value==="crop"&&documentState.value)Object.assign(crop,{x:0,y:0,width:documentState.value.width,height:documentState.value.height,ratio:"free"});}
function constrainCrop(){const doc=documentState.value;if(!doc)return;const ratios:any={"1:1":1,"4:3":4/3,"16:9":16/9},ratio=ratios[crop.ratio];if(ratio)crop.height=Math.round(crop.width/ratio);crop.x=Math.max(0,Math.min(Math.round(crop.x),doc.width-1));crop.y=Math.max(0,Math.min(Math.round(crop.y),doc.height-1));crop.width=Math.max(1,Math.min(Math.round(crop.width),doc.width-crop.x));crop.height=Math.max(1,Math.min(Math.round(crop.height),doc.height-crop.y));}
function cropDown(event:PointerEvent,handle="move"){event.stopPropagation();if(!documentState.value)return;cropDrag={handle,x:event.clientX,y:event.clientY,start:{...crop},id:event.pointerId};(event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);}
function cropMove(event:PointerEvent){if(!cropDrag||!documentState.value||!content.value)return;const rect=content.value.getBoundingClientRect(),sx=documentState.value.width/rect.width,sy=documentState.value.height/rect.height,dx=(event.clientX-cropDrag.x)*sx,dy=(event.clientY-cropDrag.y)*sy,s=cropDrag.start,min=8;let l=s.x,r=s.x+s.width,t=s.y,b=s.y+s.height;if(cropDrag.handle==="move"){crop.x=Math.max(0,Math.min(documentState.value.width-s.width,Math.round(s.x+dx)));crop.y=Math.max(0,Math.min(documentState.value.height-s.height,Math.round(s.y+dy)));return;}if(cropDrag.handle.includes("w"))l=Math.min(r-min,Math.max(0,l+dx));if(cropDrag.handle.includes("e"))r=Math.max(l+min,Math.min(documentState.value.width,r+dx));if(cropDrag.handle.includes("n"))t=Math.min(b-min,Math.max(0,t+dy));if(cropDrag.handle.includes("s"))b=Math.max(t+min,Math.min(documentState.value.height,b+dy));Object.assign(crop,{x:Math.round(l),y:Math.round(t),width:Math.round(r-l),height:Math.round(b-t)});constrainCrop();}
function cropUp(){cropDrag=null;}
function fit(){nextTick(()=>{const doc=documentState.value,v=viewport.value;if(!doc||!v)return;zoom.value=Math.max(.04,Math.min(1,(v.clientWidth-40)/doc.width,(v.clientHeight-40)/doc.height));zoomMode.value="fit";pan.x=0;pan.y=0;});}
function actual(){zoom.value=1;zoomMode.value="actual";pan.x=0;pan.y=0;}function changeZoom(factor:number,anchor?:PointerEvent){const v=viewport.value;if(!v)return;const next=Math.max(.05,Math.min(8,zoom.value*factor));const rect=v.getBoundingClientRect(),cx=anchor?anchor.clientX-rect.left-v.clientWidth/2:0,cy=anchor?anchor.clientY-rect.top-v.clientHeight/2:0,ratio=next/zoom.value;pan.x=cx-(cx-pan.x)*ratio;pan.y=cy-(cy-pan.y)*ratio;zoom.value=next;zoomMode.value="custom";}
function viewportDown(event:PointerEvent){if(event.button!==0&&event.button!==1)return;if(tool.value==="mosaic"&&event.button===0)return;panDrag={x:event.clientX,y:event.clientY,px:pan.x,py:pan.y,id:event.pointerId};viewport.value?.setPointerCapture(event.pointerId);}function viewportMove(event:PointerEvent){if(panDrag){pan.x=panDrag.px+event.clientX-panDrag.x;pan.y=panDrag.py+event.clientY-panDrag.y;}}function viewportUp(){panDrag=null;}function wheel(event:WheelEvent){event.preventDefault();changeZoom(event.deltaY<0?1.15:.87,event as unknown as PointerEvent);}
async function submit(){
  if(!ready.value||!model.value||!prompt.value.trim()||!loraSelectionValid.value||configurationChanged.value)return;
  saveDraft();const reference=selectedReference();
  await store.submitTask('image',model.value.model_key,prompt.value.trim(),{...options},
    undefined,reference?[reference]:undefined,draftIdentity.value);
}
async function reuse(task:MediaTask){
  const target=generationModels.value.find(item=>item.model_key===task.model);
  if(!target){store.toast("原任务使用的模型当前不可用",{type:"error",persistent:true});return;}
  const epoch=store.connectionEpoch;
  const original=task.configuration_binding??null,current=configurationFor(target.model_key);
  const currentExecution=store.deployments.find(item=>item.id===target.model_key)?.execution_binding??null;
  if(!task.execution_binding||!sameModelIdentity(original,current)||!sameModelIdentity(task.execution_binding,currentExecution)){store.toast("原任务的模型或 VAE revision 已不再是当前配置",{detail:"未复用这组参数。请恢复对应配置后重试；不会自动替换同名模型。",type:"error",persistent:true});return;}
  if((task.loras?.length??0)>1){store.toast('当前工作台仅支持一个 LoRA，未复用历史参数',{type:'error'});return;}
  const originalLora=task.loras?.[0]??null;
  let choice:typeof selectedLora.value=null;
  if(originalLora){
    const asset=loraAssets.value.find(item=>item.id===originalLora.asset_id&&item.revision===originalLora.revision);
    const base=baseFor(target.model_key);
    if(asset&&base&&!compatibilityFor(asset,target.model_key))await store.assessAssetCompatibility(asset.id,base.asset_id);
    if(epoch!==store.connectionEpoch)return;
    const evidence=asset?compatibilityFor(asset,target.model_key):null;
    if(asset&&(evidence?.verdict==='exact'||evidence?.verdict==='compatible'))choice={asset,evidence,allowed:true};
    if(!choice){store.toast('原任务的 LoRA revision 当前不可用',{detail:'不会自动替换同名或较新资产。',type:'error',persistent:true});return;}
  }
  if(epoch!==store.connectionEpoch)return;
  selectedModelKey.value=target.model_key;await nextTick();prompt.value=task.prompt;
  Object.keys(options).forEach(key=>delete options[key]);
  (target.capabilities?.options??[]).forEach((field:any)=>{options[field.key]=(task.options?.[field.key] as string|number|undefined)??field.default});
  selectedLoraKey.value=choice?loraKey(choice.asset):'';loraWeight.value=originalLora?.weight??0.8;
  draftIdentity.value=JSON.parse(JSON.stringify({configuration:original,execution:task.execution_binding}));saveDraft();
  store.toast('已恢复完整 revision 参数',{detail:`${original?`配置 r${original.config_revision}`:'原始运行绑定'}${originalLora?` · LoRA ${originalLora.revision}`:' · 无 LoRA'}`,record:false});
}
async function saveDocument(){
  const doc=documentState.value,context=documentContext();if(!doc?.dirty||!documentCurrent(doc,context))return;
  const blob=doc.blob,bytes=new Uint8Array(await blob.arrayBuffer());if(!documentCurrent(doc,context))return;
  const asset=await store.runAction(`image:save:${doc.key}`,"正在保存当前工作图",()=>desktopBridge().uploadAsset({name:`image-edit-${Date.now()}.png`,type:"image/png",bytes}),{record:false});
  if(!asset||!contextCurrent(context))return;
  const task=await store.submitTask("image","mediacenter-client-image-edit",`图片编辑 · ${doc.width}×${doc.height}`,{operation:"composite",width:doc.width,height:doc.height},[asset.id]);
  if(task&&documentCurrent(doc,context)&&doc.blob===blob){doc.savedBlob=blob;doc.dirty=false;documentState.value={...doc};loadedDocuments.set(doc.key,documentState.value);store.selectedTaskIds.image=task.id;await store.refreshSnapshot(false);}
}
async function exportDocument(){const doc=documentState.value;if(!doc)return;if(doc.taskId&&!doc.dirty){const task=imageTasks.value.find(item=>item.id===doc.taskId);if(task?.output?.artifact_url){await store.saveArtifact(task.output.artifact_url,`${doc.name.replace(/\.[^.]+$/,"").slice(0,80)}.png`);return;}}const url=URL.createObjectURL(doc.blob),link=document.createElement("a");link.href=url;link.download=`${doc.name.replace(/\.[^.]+$/,"").slice(0,80)||"mediacenter-image"}.png`;link.click();setTimeout(()=>URL.revokeObjectURL(url),60000);store.addMessage("图片已导出",{detail:link.download});}
async function upscale(){
  const doc=documentState.value,context=documentContext(),up=upscaleModels.value.find(item=>item.model_key===upscaleModelKey.value);
  if(!doc||!up?.healthy||!documentCurrent(doc,context))return;
  const bytes=new Uint8Array(await doc.blob.arrayBuffer());if(!documentCurrent(doc,context))return;
  const asset=await store.runAction(`image:upscale:upload:${doc.key}`,"正在准备超分源图",()=>desktopBridge().uploadAsset({name:`upscale-source-${Date.now()}.png`,type:doc.blob.type||"image/png",bytes}),{record:false});
  if(!asset||!contextCurrent(context))return;
  const task=await store.submitTask("image",up.model_key,"AI 超分",{},[asset.id]);if(task&&contextCurrent(context)){store.selectedTaskIds.image=task.id;tool.value="select";}
}
function onKeys(event:KeyboardEvent){if(!(event.ctrlKey||event.metaKey)||event.altKey)return;if(event.key.toLowerCase()==="z"){event.preventDefault();void(event.shiftKey?redo():undo());}else if(event.key.toLowerCase()==="y"){event.preventDefault();void redo();}else if(event.key.toLowerCase()==="s"){event.preventDefault();void saveDocument();}else if(event.key==="Enter"){event.preventDefault();void submit();}}
function historyWheel(event:WheelEvent){const target=event.currentTarget as HTMLElement;if(Math.abs(event.deltaY)>Math.abs(event.deltaX)&&target.scrollWidth>target.clientWidth){event.preventDefault();target.scrollLeft+=event.deltaY;}}
function selectGenerationModel(key:string,event:MouseEvent){selectedModelKey.value=key;const picker=(event.currentTarget as HTMLElement).closest("details");if(picker instanceof HTMLDetailsElement)picker.open=false;}
onMounted(()=>{resizeObserver=new ResizeObserver(()=>{if(zoomMode.value==="fit")fit()});if(viewport.value)resizeObserver.observe(viewport.value)});onBeforeUnmount(()=>{workspaceMounted=false;saveDraft();resizeObserver?.disconnect();Object.values(historyUrls).forEach(URL.revokeObjectURL);loadedDocuments.forEach(release)});
</script>

<template>
  <section class="workbench-view image-workspace" tabindex="-1" @keydown="onKeys">
    <header class="image-commonbar"><div><ActionButton icon="fit" title="适应画布" compact @click="fit" /><ActionButton icon="zoomOut" title="缩小" compact @click="changeZoom(.8)" /><span>{{ zoomMode === 'fit' ? '适应' : `${Math.round(zoom*100)}%` }}</span><ActionButton icon="zoomIn" title="放大" compact @click="changeZoom(1.2)" /><button type="button" @click="actual">1:1</button></div><nav><ActionButton icon="plus" title="新建图片文档" compact @click="openLocal" /><ActionButton icon="undo" title="撤销" compact :disabled="!documentState?.undo.length" @click="undo" /><ActionButton icon="redo" title="重做" compact :disabled="!documentState?.redo.length" @click="redo" /><ActionButton :action-key="documentState ? `image:save:${documentState.key}` : ''" icon="save" title="保存到历史" compact :disabled="!documentState?.dirty" @click="saveDocument" /></nav><ActionButton icon="export" label="导出" compact :disabled="!documentState" @click="exportDocument" /></header>
    <nav class="image-toolrail"><button v-for="item in [{key:'select',icon:'select',label:'选择'},{key:'crop',icon:'crop',label:'裁剪'},{key:'mosaic',icon:'mosaic',label:'马赛克'},{key:'upscale',icon:'upscale',label:'超分'}]" :key="item.key" type="button" :class="{active:tool===item.key}" :title="item.label" @click="setTool(item.key as Tool)"><AppIcon :name="item.icon as any" :size="18" /><small>{{ item.label }}</small></button></nav>
    <section class="image-properties"><template v-if="tool==='select'"><b>选择与平移</b><span>{{ documentState?.dirty ? '当前工作图有未保存修改' : '拖动画布查看，滚轮缩放' }}</span></template><template v-else-if="tool==='crop'"><b>裁剪</b><label>比例<select v-model="crop.ratio" @change="constrainCrop"><option value="free">自由</option><option value="1:1">1:1</option><option value="4:3">4:3</option><option value="16:9">16:9</option></select></label><label>X<input v-model.number="crop.x" type="number" min="0" @input="constrainCrop"></label><label>Y<input v-model.number="crop.y" type="number" min="0" @input="constrainCrop"></label><label>宽<input v-model.number="crop.width" type="number" min="1" @input="constrainCrop"></label><label>高<input v-model.number="crop.height" type="number" min="1" @input="constrainCrop"></label><button type="button" @click="discardTool">还原</button><button type="button" class="primary" :disabled="!documentState" @click="applyCrop">应用</button></template><template v-else-if="tool==='mosaic'"><b>马赛克</b><label>画笔<input v-model.number="mosaic.size" type="range" min="8" max="160"><output>{{ mosaic.size }}px</output></label><label>强度<input v-model.number="mosaic.strength" type="range" min="4" max="32"><output>{{ mosaic.strength }}</output></label><button type="button" @click="discardTool">放弃</button><button type="button" class="primary" :disabled="!mosaic.dirty" @click="applyMosaic">应用</button></template><template v-else><b>AI 超分</b><span>{{ documentState?.dirty ? '输入：当前已编辑工作图' : '输入：当前源图' }}</span><select v-model="upscaleModelKey"><option v-for="item in upscaleModels" :key="item.model_key" :value="item.model_key" :disabled="!item.healthy">{{ item.label }}{{ item.healthy?'':' · 未部署' }}</option></select><ActionButton :action-key="'submit:image'" icon="upscale" label="提交超分" tone="primary" compact :disabled="!documentState || !upscaleModels.some(item=>item.model_key===upscaleModelKey&&item.healthy)" @click="upscale" /></template></section>
    <main ref="viewport" class="image-stage" :class="{ 'drop-active': dropActive }" @pointerdown="viewportDown" @pointermove="viewportMove" @pointerup="viewportUp" @pointercancel="viewportUp" @wheel="wheel" @dragenter.prevent="dropActive=true" @dragover.prevent @dragleave.self="dropActive=false" @drop.prevent.stop="dropLocal">
      <div v-if="documentState" ref="content" class="image-content" :style="{width:`${documentState.width}px`,height:`${documentState.height}px`,transform}">
        <canvas v-show="tool==='mosaic'" ref="mosaicCanvas" @pointerdown.stop="mosaicDown" @pointermove="mosaicMove" @pointerup="mosaicUp" @pointercancel="mosaicUp" />
        <img v-show="tool!=='mosaic'" ref="imageNode" :src="documentState.url" :alt="documentState.name" draggable="false">
        <div v-if="tool==='crop'" class="crop-selection" :style="cropStyle" @pointerdown="cropDown($event)" @pointermove="cropMove" @pointerup="cropUp" @pointercancel="cropUp"><i v-for="handle in ['nw','n','ne','e','se','s','sw','w']" :key="handle" :class="handle" @pointerdown.stop="cropDown($event,handle)" /></div>
      </div>
      <div v-else class="image-empty"><AppIcon name="image" :size="32" /><b>开始创作</b><span>在右侧生成图片，或把本地图片拖入画布。</span></div>
      <div v-if="inFlight" class="image-task-overlay" role="status"><StateBadge :label="taskStatusLabel(currentTask!)" :tone="currentTask!.status==='running'?'busy':'neutral'" /><b>{{ taskStageLabel(currentTask!) }}</b><span>{{ currentTask?.status === 'queued' ? '等待可用 GPU' : taskElapsed(currentTask!) }}</span><i><em :style="{width:`${Math.round(Number(currentTask?.progress??0)*100)}%`}" /></i><ActionButton :action-key="`task:${currentTask!.id}:cancel`" icon="stop" :label="canCancelTask(currentTask!) ? '取消' : '正在停止'" :disabled="!canCancelTask(currentTask!)" :title="canCancelTask(currentTask!) ? '取消任务' : '已请求停止，等待执行退出确认'" tone="danger" compact @click="store.cancelTask(currentTask!)" /></div>
      <div v-else-if="currentTask && ['failed', 'interrupted', 'canceled'].includes(currentTask.status)" class="image-task-overlay error" role="status"><StateBadge :label="taskStatusLabel(currentTask)" :tone="currentTask.status === 'canceled' ? 'neutral' : 'error'" /><b>{{ currentTask.model }}</b><span :title="currentTask.status === 'canceled' ? '任务已取消' : taskErrorMessage(currentTask)">{{ currentTask.status === 'canceled' ? '任务已取消' : taskErrorMessage(currentTask) }}</span><div class="image-task-actions"><ActionButton :action-key="`task:${currentTask.id}:retry`" icon="refresh" label="重试" :disabled="Boolean(taskRetryDisabledReason(currentTask))" :title="taskRetryDisabledReason(currentTask) || '按原任务配置重试'" compact @click="store.retryTask(currentTask)" /><ActionButton icon="redo" label="复用参数" compact @click="reuse(currentTask)" /></div></div>
    </main>
    <aside class="image-generator"><div class="image-generator-scroll"><section><header><span><AppIcon name="models" :size="13" /></span><div><b>生成模型</b><small>当前服务器能力合同</small></div></header><details class="image-model-picker"><summary><AppIcon name="models" :size="13" /><b>{{ model?.label || '选择生成模型' }}</b><StateBadge :label="ready?'就绪':'离线'" :tone="ready?'ready':'error'" /><AppIcon name="chevron" :size="12" /></summary><div role="listbox" aria-label="生成模型"><button v-for="item in generationModels" :key="item.model_key" type="button" role="option" :aria-selected="item.model_key===selectedModelKey" :class="{active:item.model_key===selectedModelKey}" :disabled="!item.healthy" @click="selectGenerationModel(item.model_key,$event)"><AppIcon name="image" :size="13" /><span><b>{{ item.label }}</b><small>{{ item.model_id }}</small></span><i :class="{ready:item.healthy}" /></button></div></details><div class="model-inline-state"><StateBadge :label="ready?'模型就绪':'服务离线'" :tone="ready?'ready':'error'" /><small>{{ model?.model_id }} · {{ model?.gpu_indices?.length?`GPU ${model.gpu_indices.join(' + ')}`:'未分配 GPU' }}</small></div></section><div v-if="configurationChanged" class="image-binding-warning" role="alert"><AppIcon name="warning" :size="13" /><span>服务器配置已变化，草稿已保留</span><button type="button" @click="adoptConfiguration">采用当前配置</button></div>
      <div v-if="execution" class="image-bound-assets"><span>{{ vaeLabel }}</span><small>{{ configuration?.vae_asset_revision || execution.model_asset_revision }}</small></div>
      <section v-if="supportsLora" class="image-lora-section">
        <header><span><AppIcon name="link" :size="13" /></span><div><b>LoRA</b><small>与当前基础模型匹配</small></div></header>
        <select v-model="selectedLoraKey" aria-label="任务 LoRA">
          <option value="">不使用 LoRA</option>
          <option v-if="selectedLoraKey && !selectedLora" :value="selectedLoraKey" disabled>已选 revision 不可用</option>
          <option v-for="choice in availableLoras" :key="loraKey(choice.asset)" :value="loraKey(choice.asset)">{{ choice.asset.display_name }} · {{ compatibilityLabels[choice.evidence!.verdict] }}</option>
        </select>
        <div v-if="selectedLora" class="lora-weight">
          <label>权重 <input v-model.number="loraWeight" aria-label="LoRA 权重" type="number" min="-2" max="2" step="0.05"></label>
          <input v-model.number="loraWeight" aria-label="LoRA 权重滑块" type="range" min="-2" max="2" step="0.05">
          <small>{{ selectedLora.asset.revision }} · {{ compatibilityLabels[selectedLora.evidence!.verdict] }}</small>
        </div>
        <p v-if="!loraSelectionValid" class="image-binding-warning" role="alert">LoRA 不可用或权重超出 -2 至 2，请重新选择。</p>
        <p v-if="!loraAssets.length" class="lora-empty">在模型中心导入 LoRA 后即可选择。</p>
        <details v-if="unavailableLoras.length" class="lora-unavailable">
          <summary>不可用 LoRA · {{ unavailableLoras.length }}</summary>
          <div v-for="choice in unavailableLoras" :key="loraKey(choice.asset)" aria-disabled="true">
            <AppIcon :name="choice.evidence?.verdict==='incompatible'?'close':'warning'" :size="12" />
            <span>{{ choice.asset.display_name }}<small>{{ compatibilityLabels[choice.evidence?.verdict??'unknown'] }} · {{ choice.evidence?.reason_codes.includes('declared_base_identity_mismatch')?'声明底模与当前模型不同':'尚未通过当前底模兼容验证' }}</small></span>
          </div>
        </details>
      </section><section><header><span><AppIcon name="edit" :size="13" /></span><div><b>{{ capability.prompt_label || '画面描述' }}</b><small>主体、构图、材质、光线与风格</small></div></header><textarea v-model="prompt" rows="7" maxlength="4000" :placeholder="capability.prompt_placeholder" /></section><section v-if="capability.size_presets?.length"><header><span><AppIcon name="fit" :size="13" /></span><div><b>输出尺寸</b><small>模型推荐比例</small></div></header><div class="ratio-grid"><button v-for="preset in capability.size_presets" :key="`${preset.width}x${preset.height}`" type="button" :class="{active:Number(options.width)===preset.width&&Number(options.height)===preset.height}" @click="options.width=preset.width;options.height=preset.height"><b>{{ preset.label }}</b><small>{{ preset.width }}×{{ preset.height }}</small></button></div><OptionFields :fields="basicFields" :values="options" /></section><section v-else><header><span><AppIcon name="settings" :size="13" /></span><div><b>输出设置</b></div></header><OptionFields :fields="basicFields" :values="options" /></section><details v-if="advancedFields.length"><summary>高级参数</summary><OptionFields :fields="advancedFields" :values="options" /></details><div v-if="capability.notices?.length" class="contract-notices"><p v-for="notice in capability.notices" :key="notice">{{ notice }}</p></div></div><footer><div><b>{{ model?.label || '图片模型' }} · {{ options.width || '—' }}×{{ options.height || '—' }}</b><small>{{ selectedLora ? `${selectedLora.asset.display_name} · ${loraWeight}` : '基础模型' }} · Ctrl + Enter</small></div><ActionButton action-key="submit:image" icon="play" label="生成图片" tone="primary" :disabled="!ready||!prompt.trim()||!loraSelectionValid||configurationChanged" @click="submit" /></footer></aside>
    <section class="image-history"><header><div><b>历史作品</b><small>当前服务器 · {{ completedTasks.length }} 张</small></div></header><div @wheel="historyWheel"><article v-for="task in completedTasks.slice(0,50)" :key="task.id" :class="{active:currentTask?.id===task.id}"><button type="button" @click="selectTask(task)"><span><img v-if="historyUrls[task.id]" :src="historyUrls[task.id]" :alt="task.prompt"><AppIcon v-else name="image" /></span><span class="image-history-copy"><b>{{ task.model }}</b><small>{{ task.options?.width && task.options?.height ? `${task.options.width}×${task.options.height} · ` : '' }}{{ formatDate(task.created_at) }}</small></span></button><ActionButton v-if="task.model!=='mediacenter-client-image-edit'" icon="redo" title="复用参数" compact @click="reuse(task)" /></article><p v-if="!completedTasks.length">生成成功的图片会出现在这里</p></div></section>
  </section>
</template>
