"""Fixed offline SDXL SDK/PEFT layer; no downloader or daemon lifecycle.

An independently approved MC034 build result supplies the actual parent digest.
The source declaration deliberately has no invented final/parent image digest.
"""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tarfile
import time

if __package__:
    from scripts import build_runtime_image as core
else:
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    import build_runtime_image as core

SDK = core.SDK + ('mediacenter/adapters/__init__.py','mediacenter/adapters/sdxl.py','mediacenter/worker_cli.py')
PEFT_SHA = '3d129d64def3d74779c32a080d2567e5f7b674e77d546e3585138216d903f99e'
ENTRYPOINT = ['/opt/python/bin/python3.11','-B','-u','-m','mediacenter.worker_cli']
require = core.require


def document(path, limit=2*1024**2, approved=None):
    path=Path(path);raw=core.read_file(core.safe_path(path.parent,path.name),limit)
    if approved is not None:require(core.hash_bytes(raw)==approved,'independent_approval_mismatch')
    return core.strict_json(raw),core.hash_bytes(raw)


def contract(source, release_sha=None, artifacts=None):
    source=Path(source);root=source/'containers/sdxl'
    release,sha=document(root/'release.json',approved=release_sha)
    require(release['schema']==1 and release['model']=='sdxl-base-1.0' and release['sdk_version']=='mc.sdxl-sdk/1'
            and release['platform']=='linux/amd64','sdxl_contract_invalid')
    base,_=document(source/'containers/runtime-v1/release.json',approved=release['base_release_sha256'])
    require(release['builder']==base['builder'],'builder_contract_changed')
    locks={name:document(source/'containers/runtime-v1'/name,4*1024**2,base['inputs'][name])[0]
           for name in ('python.lock','system.lock')}
    delta,_=document(root/'python.lock',approved=release['inputs']['python.lock'])
    require(delta['base_python_lock_sha256']==base['inputs']['python.lock'] and len(delta['packages'])==1,'sdxl_delta_invalid')
    peft=delta['packages'][0]
    require(peft['name']=='peft' and peft['version']=='0.17.1' and peft['sha256']==PEFT_SHA
            and peft['size']==504896,'sdxl_peft_changed')
    combined=copy.deepcopy(locks['python.lock']);combined['packages']+=delta['packages'];combined['required_roots']['peft']='0.17.1'
    closure=core.validate_python_lock(combined)
    require(tuple(row['path'] for row in release['sdk'])==SDK,'sdxl_sdk_not_whitelisted')
    for row in release['sdk']:core.verify_file(source,dict(row,filename=row['path']))
    docker=core.read_file(root/'Dockerfile',65536)
    require(core.hash_bytes(docker)==release['inputs']['Dockerfile'],'sdxl_dockerfile_changed')
    require(docker.startswith(b'FROM runtime_parent\n') and b'#syntax=' not in docker,'external_frontend_forbidden')
    if artifacts is not None:
        evidence=core.verify_wheel(artifacts,peft)
        require(evidence['metadata_sha256']==peft['metadata_sha256'],'wheel_metadata_changed')
    return release,combined,locks['system.lock'],{'release_sha256':sha,'declaration':closure,
        'delta_bytes':peft['size'],'artifact_verified':artifacts is not None,'build':'not_executed','image_digest':None}


def parent_contract(source, release, receipt, approved_sha, layout):
    require(core.SHA.fullmatch(approved_sha or '') is not None,'parent_approval_required')
    result,_=document(receipt,4*1024**2,approved_sha)
    require(result.get('build')=='completed' and result.get('release_sha256')==release['base_release_sha256'],
            'parent_build_binding_mismatch')
    actual=result['actual']
    require(actual['offline_build_import']=='passed' and actual['python']['uid']==1000,'parent_import_missing')
    base,_=document(Path(source)/'containers/runtime-v1/python.lock',4*1024**2)
    require(sorted((core.normalized(n),v) for n,v in actual['python']['packages'])==
            sorted((core.normalized(p['name']),p['version']) for p in base['packages']),'parent_inventory_mismatch')
    parent={'manifest':actual['manifest'],'config':actual['config'],
            'layers':[{k:row[k] for k in ('digest','size')} for row in actual['layers']]}
    core.verify_oci(layout,parent)
    require(release['parent_manifest_digest'] in (None,parent['manifest']),'parent_digest_changed')
    return parent


def stage(source, artifacts, destination, release_sha, max_bytes):
    require(core.SHA.fullmatch(release_sha or '') is not None,'independent_release_approval_required')
    release,combined,_,report=contract(source,release_sha,artifacts)
    source=Path(source);delta,_=document(source/'containers/sdxl/python.lock')
    peft=delta['packages'][0]
    copies=[(source,dict(row,filename=row['path']),'sdk/'+row['path']) for row in release['sdk']]
    copies.append((Path(artifacts),peft,'wheels/'+peft['filename']))
    docker=core.read_file(source/'containers/sdxl/Dockerfile',65536)
    require(core.hash_bytes(docker)==release['inputs']['Dockerfile'],'input_changed')
    requirements=('peft==0.17.1 --hash=sha256:'+PEFT_SHA+'\n').encode()
    total=sum(row['size'] for _,row,_ in copies)+len(docker)+len(requirements)
    require(type(max_bytes) is int and 0<total<=max_bytes<=64*1024**3,'context_byte_budget')
    dest=core.private_output(destination)
    for root,row,name in copies:
        original=core.safe_path(root,row['filename']);output=dest/name
        output.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        count=0;sha=hashlib.sha256()
        with original.open('rb') as inp,output.open('xb') as out:
            if os.name=='posix':os.fchmod(out.fileno(),0o600)
            while True:
                chunk=inp.read(min(1024**2,row['size']-count+1))
                if not chunk:break
                count+=len(chunk);require(count<=row['size'],'input_changed');sha.update(chunk);out.write(chunk)
            out.flush();os.fsync(out.fileno())
        require(count==row['size'] and sha.hexdigest()==row['sha256'],'input_changed')
    core.write_exclusive(dest/'Dockerfile',docker);core.write_exclusive(dest/'requirements.txt',requirements)
    core.write_exclusive(dest/'context-receipt.json',core.canonical(dict(report,status='staged_not_built',payload_bytes=total)))
    core.sync_directory(dest)
    return dest


def fixed_argv(buildx,builder,endpoint,context,layout,output,mode,parent_digest,cgroup):
    require(type(parent_digest) is str and re.fullmatch('sha256:[0-9a-f]{64}',parent_digest),'parent_digest_invalid')
    require(type(cgroup) is str and re.fullmatch('/[A-Za-z0-9_/-]+',cgroup) and cgroup!='/' and '..' not in cgroup,'cgroup_invalid')
    argv=core.fixed_build_argv(buildx,builder,endpoint,context,layout,output,mode)['argv']
    index=argv.index('--build-context')+1
    argv[index]='runtime_parent=oci-layout://'+layout+'@'+parent_digest
    argv[-1:-1]=['--cgroup-parent',cgroup]
    return argv


def inspect_output(path, release, system, combined, parent, max_bytes, timeout):
    expected=copy.deepcopy(system);expected['base']['layers']=parent['layers']
    result=core.inspect_built_oci(Path(path),release,expected,combined,max_bytes,timeout)
    with tarfile.open(path,'r:') as archive:
        config_name='blobs/sha256/'+result['config'][7:]
        with archive.extractfile(config_name) as stream:raw=stream.read(1024**2+1)
        require(len(raw)<=1024**2 and core.hash_bytes(raw)==result['config'][7:],'output_config_changed')
        config=core.strict_json(raw)['config']
        require(config.get('Entrypoint')==ENTRYPOINT and config.get('Cmd') in (None,[]),'worker_entrypoint_changed')
    result['sdxl_gpu_and_lora']='not_run'
    return result


def build(source, artifacts, parent_oci, parent_result, parent_sha, preparation, preparation_sha,
          release_sha, evidence, mode, max_bytes, timeout, max_log_bytes):
    started=time.monotonic();require(sys.platform=='linux','build_requires_linux')
    require(core.SHA.fullmatch(release_sha or '') and core.SHA.fullmatch(preparation_sha or ''),'independent_approval_required')
    require(mode in ('cold','warm') and type(max_bytes) is int and 0<max_bytes<=64*1024**3
            and 0<timeout<=7200 and 0<max_log_bytes<=64*1024**2,'build_budget_invalid')
    release,combined,system,report=contract(source,release_sha,artifacts)
    parent=parent_contract(source,release,parent_result,parent_sha,parent_oci)
    prep,buildx,buildctl=core.verify_preparation(preparation,preparation_sha,release,artifacts)
    with core.builder_lock(prep['storage']['path']):
        active=Path(prep['storage']['path'])/'.runtime-build-active.json'
        require(not active.exists(),'previous_build_exit_unconfirmed')
        dest=core.private_output(evidence);config=core.private_output(dest/'client-config')
        core.write_exclusive(config/'config.json',b'{}');env=core.clean_environment(config)
        endpoint='unix://'+prep['socket']['path'];ordinal=0;samples=[]
        def observe():
            require(core.proc_identity(prep['process']['pid'])==prep['process'],'daemon_identity_changed')
            require(core.object_identity(prep['socket']['path'],'socket')==prep['socket']['identity'],'builder_socket_changed')
            require(core.object_identity(prep['storage']['path'],'directory')==prep['storage']['identity'],'builder_storage_changed')
            require({name:core.read_file(Path(prep['cgroup']['path'])/name,4096).decode().strip() for name in prep['limits']}==prep['limits'],
                    'builder_limits_changed')
            size=core.directory_bytes(prep['storage']['path'],max_bytes)+core.directory_bytes(dest,max_bytes)
            require(size<=max_bytes and len(samples)<400000,'storage_byte_budget')
            samples.append({'elapsed':time.monotonic()-started,'logical_bytes':size})
        def run(argv,execution=False):
            nonlocal ordinal
            current,_,_=core.verify_preparation(preparation,preparation_sha,release,artifacts)
            require(current==prep,'preparation_changed');remaining=timeout-(time.monotonic()-started)
            require(remaining>0,'build_timeout');ordinal+=1
            return core.run_bounded(argv,env,config,remaining,max_log_bytes,core.private_output(dest/('phase-%02d'%ordinal)),observe if execution else None)[0]
        submitted=False
        try:
            require(re.search(r'\bv0\.31\.1\b',run([buildx,'version']).decode()),'buildx_version_changed')
            info=core.strict_json(run([buildctl,'--addr',endpoint,'debug','info','--format','{{json .}}']))
            require(info['buildkitVersion']['version']=='v0.27.0','buildkit_version_changed')
            workers=core.strict_json(run([buildctl,'--addr',endpoint,'debug','workers','--format','{{json .}}']))
            require(len(workers)==1 and workers[0]['id']==prep['worker_id'] and workers[0]['buildkitVersion']['version']=='v0.27.0'
                    and not workers[0].get('gcPolicy') and any(p['os']=='linux' and p['architecture']=='amd64' for p in workers[0]['platforms']),
                    'builder_worker_changed')
            builder='mc-runtime-sdxl-'+release_sha[:16]
            run([buildx,'create','--name',builder,'--driver','remote',endpoint])
            description=run([buildx,'inspect',builder]).decode()
            require(re.search(r'(?m)^Driver:\s+remote\s*$',description) and endpoint in description,'builder_driver_changed')
            context=stage(source,artifacts,dest/'context',release_sha,max_bytes);observe()
            parent_contract(source,release,parent_result,parent_sha,parent_oci)
            output=dest/'sdxl.oci.tar'
            argv=fixed_argv(buildx,builder,endpoint,str(context),str(Path(parent_oci).absolute()),str(output),mode,parent['manifest'],prep['cgroup']['membership'])
            lease=core.canonical({'state':'submitted_or_unknown','process':prep['process'],'release_sha256':release_sha,
                'preparation_sha256':preparation_sha,'parent_result_sha256':parent_sha,'evidence':str(dest),'created_ns':time.time_ns()})
            core.write_exclusive(active,lease);submitted=True
            run(argv,True)
            current,_,_=core.verify_preparation(preparation,preparation_sha,release,artifacts)
            require(current==prep,'preparation_changed');observe()
            remaining=timeout-(time.monotonic()-started);require(remaining>0,'build_timeout')
            actual=inspect_output(output,release,system,combined,parent,max_bytes,remaining)
            require(time.monotonic()-started<=timeout,'build_timeout')
            result=dict(schema=1,build='completed',mode=mode,release_sha256=release_sha,parent_result_sha256=parent_sha,
                actual=actual,seconds=time.monotonic()-started,storage_samples=samples,network_bytes=None,
                disk_measurement='sampled logical bytes, not physical peak',daemon_execution='result_returned_no_domain_exit_claim',model_tests='not_run')
            core.write_exclusive(dest/'build-result.json',core.canonical(result))
            require(core.read_file(active,1024**2)==lease,'active_identity_changed')
            completed=active.with_name('.runtime-build-completed-'+core.hash_bytes(lease)+'.json')
            require(not completed.exists(),'completed_record_exists');active.rename(completed);core.sync_directory(completed.parent)
            return result
        except BaseException as error:
            core.write_exclusive(dest/'build-failure.json',core.canonical({'error':getattr(error,'code',type(error).__name__),
                'daemon_execution':'unknown' if submitted else 'build_not_submitted','resources':'retained_no_release_claim'}))
            raise


def export_release(source, release_sha, build_result, build_sha, archive_path, parent_result, parent_sha,
                   parent_oci, destination, release_id, artifact_url=None, template_source=None):
    """Deterministic installable declaration/draft. Never publishes or approves it."""
    from mediacenter.container_releases import RuntimeRelease
    from mediacenter.runtime_provisioning import RuntimeTemplate
    from mediacenter.runtime_artifacts import verify_oci_archive
    require(core.SHA.fullmatch(release_sha or '') and core.SHA.fullmatch(build_sha or ''),'independent_approval_required')
    release,combined,system,_=contract(source,release_sha)
    result,_=document(build_result,8*1024**2,build_sha)
    require(result.get('build')=='completed' and result.get('release_sha256')==release_sha
            and result.get('parent_result_sha256')==parent_sha,'build_result_binding_changed')
    parent=parent_contract(source,release,parent_result,parent_sha,parent_oci)
    archive=core.safe_path(Path(archive_path).parent,Path(archive_path).name)
    started=time.monotonic();size=archive.stat().st_size
    require(0<size<=32*1024**3,'output_byte_budget')
    actual=inspect_output(archive,release,system,combined,parent,32*1024**3,600)
    require(actual['manifest']==result['actual']['manifest'] and actual['config']==result['actual']['config']
            and actual['layers']==result['actual']['layers'],'build_output_changed')
    sha=hashlib.sha256();count=0
    with archive.open('rb') as stream:
        while True:
            block=stream.read(min(1024**2,size-count+1))
            if not block:break
            count+=len(block);require(count<=size and time.monotonic()-started<600,'export_budget');sha.update(block)
    require(count==size,'output_changed')
    with tarfile.open(archive,'r:') as content:
        with content.extractfile('blobs/sha256/'+actual['config'][7:]) as stream:raw=stream.read(1024**2+1)
        require(len(raw)<=1024**2 and core.hash_bytes(raw)==actual['config'][7:],'config_changed')
        config=core.strict_json(raw)['config']
    value={'schema':1,'release_id':release_id,'adapter_id':'sdxl','sdk_digest':core.hash_bytes(core.canonical(release['sdk'])),
        'image':{'reference':actual['manifest'],'image_id':actual['manifest'],'platform':'linux/amd64',
                 'entrypoint':config['Entrypoint'],'command':config.get('Cmd') or [],'environment':config.get('Env') or []},
        'artifact':{'format':'oci-layout-tar','url':artifact_url,'sha256':sha.hexdigest(),'byte_size':size}}
    manifest=None;template=None
    if artifact_url is not None:
        manifest=RuntimeRelease(value)
        with archive.open('rb') as stream:verify_oci_archive(stream,manifest)
        if template_source is not None:
            resource,_=document(template_source,65536)
            require(type(resource) is dict and set(resource)=={'resources','limits'},'template_input_invalid')
            catalog,_=document(Path(source)/'deploy/model_catalog.json',8*1024**2)
            entries=[entry for entry in catalog['models'] if entry['catalog_key']=='sdxl-base-1.0']
            require(len(entries)==1,'sdxl_catalog_missing')
            template=RuntimeTemplate(dict(schema=1,recipe_key='sdxl-base-1.0',recipe_digest=core.hash_bytes(core.canonical(entries[0])),
                release_digest=manifest.digest,**resource))
    dest=core.private_output(destination)
    report={'status':'declaration_requires_approval_and_publication' if manifest else 'incomplete_no_publication_url',
        'build_result_sha256':build_sha,'source_release_sha256':release_sha,'runtime_release':manifest.data if manifest else None,
        'runtime_release_digest':manifest.digest if manifest else None,'draft_fields':value,
        'template':template.data if template else None,'template_digest':template.digest if template else None,
        'missing':(['artifact_url'] if manifest is None else [])+(['resources_and_limits'] if template is None else []),
        'published':False,'imported':False,'env_checked':False,'generated_tested':False}
    core.write_exclusive(dest/'release-export.json',core.canonical(report))
    if manifest:core.write_exclusive(dest/'runtime-release.json',core.canonical(manifest.data))
    if template:core.write_exclusive(dest/'runtime-template.json',core.canonical(template.data))
    return report


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True);parser.add_argument('--artifacts',type=Path)
    parser.add_argument('--release-sha256');parser.add_argument('--parent-oci',type=Path)
    parser.add_argument('--parent-result',type=Path);parser.add_argument('--parent-sha256')
    parser.add_argument('--preparation',type=Path);parser.add_argument('--preparation-sha256')
    parser.add_argument('--evidence',type=Path);parser.add_argument('--mode',choices=('cold','warm'),default='cold')
    parser.add_argument('--max-bytes',type=int);parser.add_argument('--timeout',type=float);parser.add_argument('--max-log-bytes',type=int)
    parser.add_argument('--build-result',type=Path);parser.add_argument('--build-result-sha256');parser.add_argument('--oci-archive',type=Path)
    parser.add_argument('--release-id');parser.add_argument('--artifact-url');parser.add_argument('--template-source',type=Path)
    mode=parser.add_mutually_exclusive_group(required=True);mode.add_argument('--verify-only',action='store_true');mode.add_argument('--build',action='store_true');mode.add_argument('--export-release',action='store_true')
    args=parser.parse_args(argv)
    if args.verify_only:result=contract(args.source,args.release_sha256,args.artifacts)[3]
    elif args.export_release:
        require(all(getattr(args,key) is not None for key in ('release_sha256','build_result','build_result_sha256','oci_archive',
                    'parent_result','parent_sha256','parent_oci','evidence','release_id')),'export_inputs_missing')
        result=export_release(args.source,args.release_sha256,args.build_result,args.build_result_sha256,args.oci_archive,
                              args.parent_result,args.parent_sha256,args.parent_oci,args.evidence,args.release_id,args.artifact_url,args.template_source)
    else:
        require(all(getattr(args,key) is not None for key in ('artifacts','parent_oci','parent_result','parent_sha256','preparation','preparation_sha256',
                    'release_sha256','evidence','max_bytes','timeout','max_log_bytes')),'build_inputs_missing')
        result=build(args.source,args.artifacts,args.parent_oci,args.parent_result,args.parent_sha256,args.preparation,args.preparation_sha256,
                     args.release_sha256,args.evidence,args.mode,args.max_bytes,args.timeout,args.max_log_bytes)
    print(json.dumps(result,sort_keys=True));return 0


if __name__=='__main__':raise SystemExit(main())
