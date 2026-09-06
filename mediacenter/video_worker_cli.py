"""Fixed Wan 2.1 Worker entrypoint; task data cannot select imports or paths."""
from __future__ import annotations
import argparse, hashlib, json, os, re, signal, stat, threading
from pathlib import Path, PurePosixPath
from .adapter import AdapterFactory
from .adapters.sdxl import checked, regular, require
from .capabilities import worker_capability_for
from .transport import Identity
from .worker_common import digest
from .worker_journal import CommandAuthority, WorkerJournal
from .worker_runtime import WorkerRuntime

ADAPTERS = {
    "wan2.1-t2v-1.3b": ("wan21", "mediacenter.adapters.wan21", "Wan21Adapter", 1, 1, False),
    "minimax-h3-ref2va": ("h3", "mediacenter.adapters.h3", "H3Adapter", 2, 2, True),
    "ltx-2.3-distilled": ("ltx", "mediacenter.adapters.ltx", "LTXAdapter", 1, 2, False),
    "wan2.2-i2v-a14b": ("wan22", "mediacenter.adapters.wan22", "Wan22Adapter", 1, 2, True),
    "hunyuanvideo-1.5-720p-t2v": ("hunyuan15", "mediacenter.adapters.hunyuan15", "Hunyuan15Adapter", 1, 2, False),
}
FIELDS = {"schema","server_id","instance_id","worker_epoch","binding","recovery_complete",
          "clock_trusted","journal","outputs","adapter_id","redis","asset_bindings","lora_authority"}

def read_bootstrap(path="/mc-bootstrap.json"):
    path=checked(path)
    with regular(path) as stream:
        info=os.fstat(stream.fileno())
        require(os.name!="posix" or info.st_uid==os.getuid() and not stat.S_IMODE(info.st_mode)&0o077,
                "bootstrap_not_private")
        raw=stream.read(65537)
    require(len(raw)<=65536,"bootstrap_limit")
    def pairs(items):
        result={}
        for key,item in items: require(key not in result,"bootstrap_duplicate_key");result[key]=item
        return result
    value=json.loads(raw,object_pairs_hook=pairs,
                     parse_constant=lambda _value:(_ for _ in ()).throw(ValueError("bootstrap_invalid")))
    require(type(value) is dict and set(value)==FIELDS and value["schema"]==1,"bootstrap_invalid")
    identity=Identity(value["server_id"],value["instance_id"],value["worker_epoch"]); binding=value["binding"]
    require(type(binding) is dict and set(binding)=={"model_key","recipe_revision","model_asset_id",
            "model_asset_revision","image_digest","gpu_uuids","capability_digest"},"bootstrap_binding_invalid")
    model=binding.get("model_key");require(model in ADAPTERS,"bootstrap_binding_invalid")
    adapter_id,_module,_name,minimum_gpus,maximum_gpus,_inputs=ADAPTERS[model]
    require(value["recovery_complete"] is True and value["clock_trusted"] is True
            and value["adapter_id"]==adapter_id
            and binding["capability_digest"]==digest(worker_capability_for(model))
            and value["journal"]=="/mc-journal/worker.db" and value["outputs"]=="/mc-outputs"
            and value["lora_authority"]=={"path":"/mc-lora","families":[]},"bootstrap_binding_invalid")
    require(type(binding["image_digest"]) is str and re.fullmatch(r"sha256:[0-9a-f]{64}",binding["image_digest"])
            and type(binding["gpu_uuids"]) is list and minimum_gpus<=len(binding["gpu_uuids"])<=maximum_gpus
            and len(set(binding["gpu_uuids"]))==len(binding["gpu_uuids"])
            and all(re.fullmatch(r"GPU-[0-9a-fA-F-]{36}",gpu) for gpu in binding["gpu_uuids"]),"bootstrap_gpu_invalid")
    for key in ("recipe_revision","model_asset_id","model_asset_revision"):
        require(type(binding[key]) is str and ".." not in binding[key]
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}",binding[key]),"bootstrap_binding_invalid")
    assets=value["asset_bindings"]
    require(type(assets) is dict and set(assets)=={"main","dependencies"} and assets["dependencies"]=={},
            "bootstrap_assets_invalid")
    main=assets["main"]
    require(type(main) is dict and set(main)=={"asset_id","revision","manifest_digest","path"}
            and main["asset_id"]==binding["model_asset_id"] and main["revision"]==binding["model_asset_revision"]
            and type(main["manifest_digest"]) is str and re.fullmatch(r"[0-9a-f]{64}",main["manifest_digest"])
            and main["path"]=="/mc-models/assets/"+main["asset_id"],"bootstrap_assets_invalid")
    endpoint=value["redis"]; socket=PurePosixPath(endpoint["unix_socket"])
    require(set(endpoint)=={"username","secret_file","unix_socket"}
            and endpoint["username"]=="mc_w_"+digest([identity.server_id,identity.instance_id,identity.worker_epoch])
            and endpoint["secret_file"]=="/mc-worker-secret" and socket.parent==PurePosixPath("/mc-redis"),
            "bootstrap_redis_invalid")
    return value,identity,hashlib.sha256(raw).hexdigest()

def create_runtime(path="/mc-bootstrap.json"):
    value,identity,evidence=read_bootstrap(path)
    from .redis_transport import RedisEndpoint,RedisTransport
    endpoint=RedisEndpoint(**value["redis"]);endpoint.options();checked(Path(value["journal"]).parent);checked(value["outputs"])
    model=value["binding"]["model_key"];adapter_id,module,name,_min,_max,needs_inputs=ADAPTERS[model]
    if needs_inputs:checked("/mc-inputs")
    capability=worker_capability_for(model);journal=WorkerJournal(value["journal"],identity.instance_id,{model:capability})
    transport=RedisTransport(identity,endpoint,role="worker",consumer="worker-"+identity.worker_epoch)
    options=dict(binding=value["binding"],asset_bindings=value["asset_bindings"],outputs=value["outputs"])
    if needs_inputs:options["inputs"]="/mc-inputs"
    factory=AdapterFactory(module,name,options)
    try:
        runtime=WorkerRuntime(identity,value["binding"],journal,factory,transport,
            command_authority=CommandAuthority(identity,digest(value["binding"]),evidence))
        runtime.admit(evidence=evidence,recovery_complete=True,clock_trusted=True);return runtime,transport
    except BaseException:
        if "runtime" in locals():runtime.close()
        transport.close();raise

def main(argv=None):
    argparse.ArgumentParser(description=__doc__).parse_args(argv);stop=threading.Event()
    for number in (signal.SIGINT,signal.SIGTERM):signal.signal(number,lambda *_:stop.set())
    runtime,transport=create_runtime()
    try:runtime.start();stop.wait()
    finally:runtime.close();transport.close()
if __name__=="__main__":main()
