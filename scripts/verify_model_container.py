"""Explicit bounded image or speech smoke actions through the authenticated API.

No Engine/SSH/daemon control, installation, download resolver or shell commands.
An independently approved plan and private API key are required for execution.
Verify-only never connects. Timing is client-observed, not GPU kernel timing.
"""
from __future__ import annotations
import argparse
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
from urllib.parse import urlsplit

if __package__:
    from scripts import build_runtime_image as core
else:
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    import build_runtime_image as core


def check_plan(value):
    fields={'schema','run_id','server_url','instance_id','binding_digest','image_digest','release_digest','gpu_uuids',
            'installation_id','installation_mode','task','lora_a','lora_b','timeout_seconds','max_response_bytes','max_total_bytes'}
    core.require(type(value) is dict and set(value)==fields and value['schema']==1,'smoke_plan_invalid')
    for key in ('run_id','instance_id','installation_id'):
        core.require(type(value[key]) is str and re.fullmatch('[A-Za-z0-9_.-]{1,96}',value[key])
                     and '..' not in value[key],'smoke_identity_invalid')
    for key in ('binding_digest','release_digest'):
        core.require(type(value[key]) is str and core.SHA.fullmatch(value[key]),'smoke_binding_invalid')
    core.require(type(value['image_digest']) is str and re.fullmatch('sha256:[0-9a-f]{64}',value['image_digest']),'smoke_image_missing')
    parsed=urlsplit(value['server_url'])
    core.require(parsed.scheme in ('http','https') and parsed.hostname and not parsed.username and not parsed.password
                 and parsed.path in ('','/') and not parsed.query and not parsed.fragment
                 and (parsed.scheme=='https' or parsed.hostname in ('127.0.0.1','localhost','::1')),'smoke_server_invalid')
    core.require(type(value['gpu_uuids']) is list and 1<=len(value['gpu_uuids'])<=8 and
                 all(type(x) is str and re.fullmatch('GPU-[0-9a-fA-F-]{36}',x) for x in value['gpu_uuids'])
                 and len(set(value['gpu_uuids']))==len(value['gpu_uuids']),'smoke_gpu_missing')
    core.require(value['installation_mode'] in ('empty_install','existing_asset_acceptance'),'smoke_installation_mode_invalid')
    task=value['task']
    core.require(type(task) is dict and set(task)=={'service','model','prompt','options','inputs'}
                 and task['service'] in ('image','speech') and task['model']==value['instance_id']
                 and task['inputs']==[],'smoke_task_invalid')
    # Use the same pure model contract as the Worker; no model imports.
    from mediacenter.capabilities import validate_worker_request
    operation = 'image.generate' if task['service']=='image' else 'speech.generate'
    model_key = 'sdxl-base-1.0' if task['service']=='image' else task['model']
    validate_worker_request(model_key,operation,dict(task['options'],prompt=task['prompt']),[],[])
    if task['service']=='image':
        core.require(all(k in task['options'] for k in ('width','height','seed','steps')),'smoke_parameters_missing')
        for key in ('lora_a','lora_b'):
            validate_worker_request(model_key,operation,dict(task['options'],prompt=task['prompt']),[],[value[key]])
        core.require((value['lora_a']['asset_id'],value['lora_a']['revision'])!=(value['lora_b']['asset_id'],value['lora_b']['revision']),
                     'smoke_distinct_loras_required')
    else:
        core.require(value['lora_a'] is None and value['lora_b'] is None and set(task['options'])=={'speed'},
                     'smoke_speech_contract_invalid')
    for key,low,high in (('timeout_seconds',10,7200),('max_response_bytes',1024,256*1024**2),('max_total_bytes',1024,1024**3)):
        core.require(type(value[key]) is int and low<=value[key]<=high,'smoke_budget_invalid')
    return value


class Client:
    def __init__(self, plan, secret, deadline):
        self.plan,self.secret,self.deadline=plan,secret,deadline;self.bytes=0
    def request(self,method,path,body=None,key=None):
        core.require(path.startswith('/api/v1/') and '..' not in path and not any(x in path for x in ('?','#','\r','\n')),'smoke_path_invalid')
        remaining=self.deadline-time.monotonic();core.require(remaining>0,'smoke_timeout')
        target=urlsplit(self.plan['server_url']);factory=http.client.HTTPSConnection if target.scheme=='https' else http.client.HTTPConnection
        connection=factory(target.hostname,target.port,timeout=min(5,remaining))
        headers={'X-API-Key':self.secret,'Accept':'application/json'}
        if key:headers['Idempotency-Key']=key
        data=None if body is None else core.canonical(body)
        if data is not None:headers['Content-Type']='application/json'
        try:
            connection.request(method,path,data,headers);response=connection.getresponse()
            core.require(response.status in (200,201,202),'smoke_http_'+str(response.status))
            limit=min(self.plan['max_response_bytes'],self.plan['max_total_bytes']-self.bytes)
            core.require(limit>0,'smoke_total_byte_limit')
            raw=response.read(limit+1);self.bytes+=len(raw)
            core.require(len(raw)<=limit and response.length in (None,0),'smoke_response_limit_or_truncated')
            core.require(time.monotonic()<self.deadline,'smoke_timeout')
            return raw if path.startswith('/api/v1/artifacts/') else core.strict_json(raw)
        finally:connection.close()


def decode(raw, expected, sha, service='image'):
    if service == 'speech':
        import wave
        core.require(hashlib.sha256(raw).hexdigest()==sha,'smoke_artifact_hash')
        with wave.open(io.BytesIO(raw),'rb') as reader:
            metadata={'format':'WAV','sample_rate':reader.getframerate(),'channels':reader.getnchannels(),
                      'sample_count':reader.getnframes(),'bits_per_sample':reader.getsampwidth()*8,'bytes':len(raw),'sha256':sha}
            core.require(metadata['sample_rate']==24000 and 1<=metadata['channels']<=8
                         and metadata['sample_count']>0 and metadata['bits_per_sample']==16,'smoke_audio_contract')
            decoded=0
            while block:=reader.readframes(min(metadata['sample_rate'],metadata['sample_count']-decoded)):
                core.require(len(block)%(metadata['channels']*2)==0,'smoke_audio_frame_invalid')
                decoded+=len(block)//(metadata['channels']*2)
            core.require(decoded==metadata['sample_count'],'smoke_audio_truncated')
        metadata['duration_ms']=round(metadata['sample_count']*1000/metadata['sample_rate'])
        return metadata
    from PIL import Image
    core.require(hashlib.sha256(raw).hexdigest()==sha,'smoke_artifact_hash')
    stream=io.BytesIO(raw)
    with Image.open(stream) as image:
        core.require(image.format=='PNG' and image.size==(expected['width'],expected['height']),'smoke_artifact_dimensions')
        image.verify()
    stream.seek(0)
    with Image.open(stream) as image:image.load()
    return {'format':'PNG','width':expected['width'],'height':expected['height'],'sha256':sha,'bytes':len(raw)}


def exercise(plan, client, evidence):
    """Existing Server routes only. Unknown results retain every original identity."""
    dest=core.private_output(evidence);steps=[];ordinal=0
    core.write_exclusive(dest/'intent.json',core.canonical({'plan':plan,'state':'running','resource_exit':'not_claimed'}))
    def get(path):return client.request('GET',path)
    def post(path,body,label):
        nonlocal ordinal
        ordinal+=1;key=plan['run_id']+'-'+label
        core.write_exclusive(dest/('%02d-command.json'%ordinal),core.canonical({'method':'POST','path':path,'body':body,'idempotency_key':key}))
        return client.request('POST',path,body,key)
    def target():
        value=get('/api/v1/deployments/'+plan['instance_id']+'/validation-target')
        core.require(all(value[key]==plan[key] for key in ('instance_id','binding_digest','image_digest','release_digest'))
                     and value['policy']['gpus']==plan['gpu_uuids'] and value['policy']['residency']=='resident','smoke_target_changed')
        return value
    def wait_check(record):
        while True:
            record=get('/api/v1/runtime-validations/'+record['validation_id'])
            core.require(record['binding_digest']==plan['binding_digest'],'smoke_validation_binding_changed')
            if record['state']!='pending':break
            core.require(time.monotonic()<client.deadline,'smoke_timeout');time.sleep(.1)
        core.require(record['state']=='passed','smoke_validation_'+record['state'])
        return record
    def generated(label,loras):
        started=time.monotonic();target()
        task=dict(plan['task'],loras=loras)
        record=post('/api/v1/deployments/'+plan['instance_id']+'/validate',
                    {'kind':'generated_tested','binding_digest':plan['binding_digest'],'task':task},label)
        done=wait_check(record)
        core.require(done['claim_id']==load['claim_id'] and done['epoch']==load['epoch'],'warm_epoch_changed')
        result=get('/api/v1/tasks/'+done['task_id'])
        core.require(result['status']=='succeeded' and result['current_attempt_id']==done['attempt_id']
                     and result['options']['seed']==task['options']['seed'] and result['loras']==loras,'smoke_task_changed')
        artifact=result['output'];raw=get(artifact['artifact_url'])
        media=decode(raw,task['options'],artifact['sha256'],task['service'])
        entry={'phase':label,'validation':done,'media':media,'seconds':time.monotonic()-started,'timing':'client_observed_wall_seconds'}
        steps.append(entry);core.write_exclusive(dest/(label+'.json'),core.canonical(entry))
        return done
    try:
        initial=target();core.require(initial['claim_id'] is None,'smoke_requires_cold_unclaimed_instance')
        installation=get('/api/v1/service-installations/'+plan['installation_id'])
        core.require(installation['state']=='ready' and installation['options']['deployment_id']==plan['instance_id'],'smoke_installation_mismatch')
        core.write_exclusive(dest/'installation.json',core.canonical({'declared_mode':plan['installation_mode'],'record':installation,
            'installation_executed_by_this_tool':False}))
        started=time.monotonic()
        load=wait_check(post('/api/v1/deployments/'+plan['instance_id']+'/validate',
            {'kind':'env_checked','binding_digest':plan['binding_digest'],'version':initial['policy_version']},'cold-load'))
        steps.append({'phase':'cold-load','validation':load,'seconds':time.monotonic()-started,'timing':'client_observed_wall_seconds'})
        core.write_exclusive(dest/'cold-load.json',core.canonical(steps[-1]))
        generated('first-'+plan['task']['service'],[]);generated('second-'+plan['task']['service'],[])
        if plan['task']['service']=='image':
            generated('lora-a',[plan['lora_a']]);generated('lora-none',[]);generated('lora-b',[plan['lora_b']])
            cancel_task=dict(plan['task'],options=dict(plan['task']['options'],steps=80),loras=[])
        else:
            cancel_task=dict(plan['task'],prompt=(plan['task']['prompt']+' ')*64,loras=[])
        cancellation=post('/api/v1/deployments/'+plan['instance_id']+'/validate',
            {'kind':'generated_tested','binding_digest':plan['binding_digest'],'task':cancel_task},'cancel-in-flight')
        while True:
            task=get('/api/v1/tasks/'+cancellation['task_id'])
            if task['status']=='running':break
            core.require(task['status'] not in ('succeeded','failed','canceled','interrupted'),'cancel_not_observed_in_flight')
            core.require(time.monotonic()<client.deadline,'smoke_timeout');time.sleep(.05)
        original_attempt=task['current_attempt_id'];started=time.monotonic()
        post('/api/v1/tasks/'+task['id']+'/cancel',{},'cancel')
        while True:
            task=get('/api/v1/tasks/'+task['id'])
            core.require(task['current_attempt_id']==original_attempt,'cancel_attempt_changed')
            if task['status']=='canceled' and task['attempt'] and task['attempt']['exit_confirmed'] and task['attempt']['exit_evidence']:break
            core.require(time.monotonic()<client.deadline,'smoke_timeout');time.sleep(.1)
        steps.append({'phase':'cancellation','task_id':task['id'],'attempt':task['attempt'],'seconds':time.monotonic()-started})
        core.write_exclusive(dest/'cancellation.json',core.canonical(steps[-1]))
        generated('post-cancel-'+plan['task']['service'],[])
        result={'schema':1,'status':'managed_api_checks_completed','steps':steps,'http_bytes':client.bytes,
                'gpu_peak_bytes':None,'gpu_kernel_timing':'not_measured','lora_numerical_equivalence':'not_proven',
                'server_restart_disconnect':'not_run','actual_model_acceptance':'requires_complete_resource_evidence',
                'runtime_left_resident':True,'domain_exit':'not_claimed'}
        core.write_exclusive(dest/'result.json',core.canonical(result));return result
    except BaseException as error:
        core.write_exclusive(dest/'failure.json',core.canonical({'error':getattr(error,'code',type(error).__name__),
            'execution':'unknown_or_incomplete','steps':steps,'resources':'retained_no_release_claim'}))
        raise


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--plan',type=Path,required=True)
    parser.add_argument('--plan-sha256');parser.add_argument('--api-key-file',type=Path);parser.add_argument('--evidence',type=Path)
    mode=parser.add_mutually_exclusive_group(required=True);mode.add_argument('--verify-only',action='store_true');mode.add_argument('--execute',action='store_true')
    args=parser.parse_args(argv)
    raw=core.read_file(core.safe_path(args.plan.parent,args.plan.name),65536)
    if args.execute:
        core.require(core.SHA.fullmatch(args.plan_sha256 or '') and core.hash_bytes(raw)==args.plan_sha256,'smoke_independent_approval_required')
    plan=check_plan(core.strict_json(raw))
    if args.verify_only:
        print(json.dumps({'status':'plan_only','plan_sha256':core.hash_bytes(raw),'execution':'not_run'}));return 0
    core.require(args.api_key_file is not None and args.evidence is not None,'smoke_execution_inputs_missing')
    path=core.safe_path(args.api_key_file.parent,args.api_key_file.name);info=path.stat()
    core.require(os.name!='posix' or info.st_uid==os.getuid() and not stat.S_IMODE(info.st_mode)&0o077,'smoke_key_not_private')
    secret=core.read_file(path,4096).decode().strip()
    core.require(secret and '\n' not in secret and '\r' not in secret,'smoke_key_invalid')
    # Check the chosen decoder before any managed GPU action.
    if plan['task']['service']=='image':
        from PIL import Image  # noqa: F401
    else:
        import wave  # noqa: F401
    result=exercise(plan,Client(plan,secret,time.monotonic()+plan['timeout_seconds']),args.evidence)
    print(json.dumps(result,sort_keys=True));return 0


if __name__=='__main__':raise SystemExit(main())
