"""Fixed container entrypoint. No Server modules, environment discovery or plugins."""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import signal
import stat
import threading
from pathlib import Path, PurePosixPath

from .adapters.sdxl import read_json, checked, regular, require
from .adapter import AdapterFactory
from .capabilities import worker_capability_for
from .transport import Identity
from .worker_common import digest
from .worker_journal import CommandAuthority, WorkerJournal
from .worker_runtime import WorkerRuntime

SDK_VERSION = 'mc.sdxl-sdk/1'
BOOTSTRAP_FIELDS = {'schema', 'server_id', 'instance_id', 'worker_epoch', 'binding', 'recovery_complete',
                    'clock_trusted', 'journal', 'outputs', 'adapter_id', 'redis', 'asset_bindings', 'lora_authority'}


def read_bootstrap(path='/mc-bootstrap.json'):
    path = checked(path)
    with regular(path) as stream:
        info = os.fstat(stream.fileno())
        require(os.name != 'posix' or info.st_uid == os.getuid() and not info.st_mode & 0o077,
                'bootstrap_not_private')
        raw = stream.read(65537)
    require(len(raw) <= 65536, 'bootstrap_limit')
    # Parse the same captured bytes using the same strict duplicate-key parser.
    import json
    def pairs(items):
        value = {}
        for key, item in items:
            require(key not in value, 'bootstrap_duplicate_key'); value[key] = item
        return value
    value = json.loads(raw, object_pairs_hook=pairs)
    require(type(value) is dict and set(value) == BOOTSTRAP_FIELDS and type(value['schema']) is int
            and value['schema'] == 1, 'bootstrap_invalid')
    identity = Identity(value['server_id'], value['instance_id'], value['worker_epoch'])
    require(value['recovery_complete'] is True and value['clock_trusted'] is True, 'bootstrap_not_admitted')
    require(value['adapter_id'] == 'sdxl' and value['journal'] == '/mc-journal/worker.db'
            and value['outputs'] == '/mc-outputs', 'bootstrap_entrypoint_invalid')
    binding = value['binding']
    require(type(binding) is dict and set(binding) == {'model_key', 'recipe_revision', 'model_asset_id',
            'model_asset_revision', 'image_digest', 'gpu_uuids', 'capability_digest'}
            and binding['model_key'] == 'sdxl-base-1.0'
            and binding['capability_digest'] == digest(worker_capability_for('sdxl-base-1.0')), 'bootstrap_binding_invalid')
    for key in ('recipe_revision', 'model_asset_id', 'model_asset_revision'):
        require(type(binding[key]) is str and '..' not in binding[key] and re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}', binding[key]), 'bootstrap_binding_invalid')
    require(type(binding['image_digest']) is str and re.fullmatch('sha256:[0-9a-f]{64}', binding['image_digest'])
            and type(binding['gpu_uuids']) is list and 1 <= len(binding['gpu_uuids']) <= 8
            and all(type(gpu) is str and re.fullmatch('GPU-[0-9a-fA-F-]{36}', gpu) for gpu in binding['gpu_uuids'])
            and len(set(binding['gpu_uuids'])) == len(binding['gpu_uuids']), 'bootstrap_gpu_invalid')
    authority = value['lora_authority']
    require(authority == {'path': '/mc-lora', 'families': ['sdxl']}, 'bootstrap_lora_authority_invalid')
    assets = value['asset_bindings']
    require(type(assets) is dict and set(assets) == {'main', 'dependencies'} and assets['dependencies'] == {}, 'bootstrap_assets_invalid')
    main = assets['main']
    require(type(main) is dict and set(main) == {'asset_id', 'revision', 'manifest_digest', 'path'}
            and main['asset_id'] == binding['model_asset_id'] and main['revision'] == binding['model_asset_revision']
            and type(main['manifest_digest']) is str and re.fullmatch('[0-9a-f]{64}', main['manifest_digest'])
            and main['path'] == '/mc-models/assets/' + main['asset_id'], 'bootstrap_assets_invalid')
    endpoint = value['redis']
    require(type(endpoint) is dict and set(endpoint) == {'username', 'secret_file', 'unix_socket'}
            and endpoint['username'] == 'mc_w_' + digest([identity.server_id,identity.instance_id,identity.worker_epoch])
            and endpoint['secret_file'] == '/mc-worker-secret' and type(endpoint['unix_socket']) is str,
            'bootstrap_redis_invalid')
    socket = PurePosixPath(endpoint['unix_socket'])
    require(socket.parent == PurePosixPath('/mc-redis') and str(socket) == endpoint['unix_socket']
            and socket.name not in ('.', '..'), 'bootstrap_redis_invalid')
    return value, identity, hashlib.sha256(raw).hexdigest()


def create_runtime(path='/mc-bootstrap.json'):
    value, identity, evidence = read_bootstrap(path)
    # Third-party Redis import is not a model import; capability handshake stays light.
    from .redis_transport import RedisEndpoint, RedisTransport
    endpoint = RedisEndpoint(**value['redis'])
    endpoint.options()  # Validate the real private credential before journal creation.
    checked(Path(value['journal']).parent); checked(value['outputs']); checked('/mc-lora')
    journal = WorkerJournal(value['journal'], identity.instance_id,
                            {'sdxl-base-1.0': worker_capability_for('sdxl-base-1.0')})
    transport = RedisTransport(identity, endpoint, role='worker', consumer='worker-' + identity.worker_epoch)
    factory = AdapterFactory('mediacenter.adapters.sdxl', 'SDXLAdapter', dict(binding=value['binding'],
        asset_bindings=value['asset_bindings'], outputs=value['outputs'], lora_directory='/mc-lora'))
    try:
        runtime = WorkerRuntime(identity, value['binding'], journal, factory, transport,
            command_authority=CommandAuthority(identity, digest(value['binding']), evidence))
        runtime.admit(evidence=evidence, recovery_complete=True, clock_trusted=True)
        return runtime, transport
    except BaseException:
        if 'runtime' in locals(): runtime.close()
        transport.close()
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description='Fixed SDXL Worker')
    parser.parse_args(argv)  # No task-controlled config, paths or module switches.
    stop = threading.Event()
    for number in (signal.SIGINT, signal.SIGTERM):
        signal.signal(number, lambda *_: stop.set())
    runtime, transport = create_runtime()
    try:
        runtime.start()
        stop.wait()
    finally:
        runtime.close()
        transport.close()


if __name__ == '__main__':
    main()
