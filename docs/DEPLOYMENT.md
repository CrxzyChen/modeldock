# Server installation and upgrades — 1.2.28 Alpha

The PC client and Linux server are separate. The server schedules independent model containers and owns model assets, tasks and outputs. Installing Studio does not install this server.

**Current boundary:** the release provides a transactional control-server installer, not an unattended fresh-host GPU installer. Do not run it on an existing unmanaged deployment to adopt that deployment implicitly. Back up configuration, database and user assets before maintenance.

## Prerequisites

- Linux with a supported GPU driver and container execution environment.
- Python 3.10+ and the control dependencies pinned in `requirements/control-runtime.lock`.
- Host `/usr/bin/ffmpeg` and `/usr/bin/ffprobe` for output validation. Verify both executables before startup (for example, `/usr/bin/ffmpeg -version` and `/usr/bin/ffprobe -version`). A healthy API does not prove these decoders are available.
- Private Redis with the limits and ACL scheme in `deploy/redis.conf` and `deploy/redis-acl.template`.
- Explicit GPU index/UUID configuration; no GPUs are implicitly reserved for another product.
- Operator-built and verified OCI images plus a runtime configuration referring to those exact artifacts.

## Entry points

`scripts/build_server_release.py --help` builds the source-based server bundle. `scripts/install_server_release.py --help` documents install/upgrade operations and their preflight controls. `deploy/mediacenter.env.example` is a substitution template, not an immediately runnable configuration.

Review `scripts/build_runtime_image.py`, `scripts/build_derived_oci_image.py` and `deploy/production-images/` for fixed runtime construction. Historical release descriptions must be reviewed and regenerated for your own source/build. They do not supply a current production image catalog. Do not weaken integrity checks to make an old image descriptor pass.

The server entry point is `python -m mediacenter.server`. The public health endpoint is `/healthz`; authenticated routes are under `/api/v1/`. Real service availability requires the full runtime configuration, not just starting the HTTP process.

## 1. Download and verify

Obtain `MediaCenter-server-1.2.28.tar.gz` and `SHA256SUMS.txt` from the [Alpha release](https://github.com/CrxzyChen/modeldock/releases/tag/v1.2.28-alpha.1). The bundle retains the installer's existing name contract. In a new working directory on Linux:

```sh
sha256sum --check --ignore-missing SHA256SUMS.txt
git clone --branch v1.2.28-alpha.1 --depth 1 https://github.com/CrxzyChen/modeldock.git modeldock
cd modeldock
```

Stop if the bundle is missing or the checksum fails. The release checksums provide integrity, not a code-signing identity. The source checkout supplies preparation tools that are not all part of the compact server bundle.

## 2. Prepare the host and control Python

Use a dedicated Linux account with a functioning `systemctl --user` session. The current engine adapter requires the Docker-compatible API and cgroups expected by the runtime configuration. Access to the Docker socket is privileged; do not expose it to the network. Install and verify the GPU driver/container integration before enabling model services. Use `nvidia-smi --query-gpu=index,uuid,memory.total --format=csv` to obtain the actual GPU list.

For a new installation, have the administrator create `/srv/mediacenter` owned by the service account. Then, as that account:

```sh
python3 -m venv /srv/mediacenter/control-runtime-v2
/srv/mediacenter/control-runtime-v2/bin/python -m pip install \
  --require-hashes --only-binary=:all: -r requirements/control-runtime.lock
python3 scripts/install_server_release.py preflight \
  --bundle ../MediaCenter-server-1.2.28.tar.gz \
  --data-root /srv/mediacenter \
  --control-python /srv/mediacenter/control-runtime-v2/bin/python
```

Preflight verifies the bundle and disk/path constraints; it does **not** prove Docker, Redis or GPU readiness. All `/srv/mediacenter` paths here are examples for a new host, not instructions to overwrite an existing installation.

## 3. Stage the control service without starting it

Choose the GPU indices and corresponding UUIDs explicitly. Replace the two shell variables below with values from this host; they must describe the same pool.

```sh
GPU_INDICES='0'
GPU_UUIDS='REPLACE_WITH_ACTUAL_GPU_UUID'
python3 scripts/install_server_release.py install \
  --bundle ../MediaCenter-server-1.2.28.tar.gz \
  --data-root /srv/mediacenter \
  --control-python /srv/mediacenter/control-runtime-v2/bin/python \
  --gpu-pool "$GPU_INDICES" --gpu-uuids "$GPU_UUIDS" \
  --host 127.0.0.1 --port 8787 --no-service-control
```

This creates `releases/1.2.28`, the `current` link, private `config/api-key`, `config/mediacenter.env`, user systemd unit files and an installation receipt. `--no-service-control` deliberately does not start or health-check the server. An `installed` status at this point is **not** a ready model service. Existing environment/key files are retained by the installer.

## 4. Prepare Redis and model runtime — required operator step

Before starting the service, complete these bindings:

| Component | Required result |
| --- | --- |
| Private Redis | Container named `mediacenter-redis`, private Unix socket, persistent reliable streams, bounded memory, private ACL/credential files |
| Worker images | Verified OCI artifacts and release descriptors matching the exact packaged Worker source, compatible GPU/runtime dependencies |
| Runtime configuration | Host engine identity, explicit GPU UUIDs, approved artifact digests, local artifact roots, publisher credentials and templates |
| Server environment | `MEDIACENTER_RUNTIME_CONFIG` points to the private runtime JSON; model catalog/store paths refer to this host |

Use `scripts/prepare_private_redis.py --help` and `scripts/prepare_redis_server_secrets.py --help` for exclusive creation of private material, then `scripts/verify_private_redis.py --help` for verification. These helpers do not create/start the Docker container. Its mounts, user ownership and socket permissions must match the generated configuration. `deploy/mediacenter-redis.service` assumes that named container already exists.

`scripts/prepare_production_runtime.py --help` describes runtime assembly inputs. It depends on verified image/engine evidence and a deployment catalog/database; it is **not** a fresh-host one-command installer. Historical image and engine values must be checked against the new host, not blindly copied. Keep the resulting runtime JSON and credentials private (0600), and set `MEDIACENTER_RUNTIME_CONFIG` in `config/mediacenter.env` before starting. Do not replace missing verification with fabricated digests.

Model weights are downloaded/imported separately after a suitable runtime is available. Model authorization and licenses remain the operator's responsibility. There is no public prebuilt Worker registry in this Alpha; if you cannot supply these artifacts, stop here rather than treating the staged control plane as production-ready.

## 5. Start and connect Studio

After all runtime prerequisites pass:

```sh
systemctl --user daemon-reload
systemctl --user enable --now mediacenter.service
systemctl --user status mediacenter.service mediacenter-redis.service
curl --fail http://127.0.0.1:8787/healthz
```

The server unit requires the Redis unit. For startup without an interactive login, ask the administrator to enable lingering for the service account. Retrieve `config/api-key` locally and securely enter it in Studio; do not paste it into public logs/issues or commit it. Configure the reachable server URL in Studio; `127.0.0.1` on a remote PC is not the Linux server.

Acceptance order: health reports version 1.2.28 → authenticated client synchronization → hardware inventory matches the host → available runtime/installation recipes → one small real model task completes and its result opens. Health alone is not generation acceptance. Inspect failures with `journalctl --user -u mediacenter.service -n 100 --no-pager`, redacting credentials and personal data before sharing.

## Upgrades and rollback

For installations already managed by this installer, quiesce tasks and back up state/configuration first. Reuse the same data root and control Python:

```sh
python3 scripts/install_server_release.py status --data-root /srv/mediacenter
python3 scripts/install_server_release.py upgrade \
  --bundle /absolute/path/to/NEW-server-bundle.tar.gz \
  --data-root /srv/mediacenter \
  --control-python /srv/mediacenter/control-runtime-v2/bin/python
```

Upgrade requires a different version, switches the release and checks health; it preserves existing environment/key files. Coordinate Worker image updates when the protocol/SDK changes. It does not automatically refresh all Worker containers or install new Python dependencies. Review the new release notes before switching.

```sh
python3 scripts/install_server_release.py rollback \
  --data-root /srv/mediacenter \
  --control-python /srv/mediacenter/control-runtime-v2/bin/python
```

Rollback requires a retained previous release. It switches application files, not a full database/model/runtime snapshot restore. Confirm data and Worker compatibility first. Never delete model directories as part of application upgrade/rollback.

## Public access

Put a TLS reverse proxy in front of the server. `deploy/nginx-mediacenter-sse.conf` documents SSE handling. Keep buffering/caching disabled for `/api/v1/events`. Set upload limits according to the actual client transfer protocol and your capacity.

For an internal server behind NAT, an operator may establish a restricted reverse SSH tunnel to a public proxy. Keep the reverse listener bound to loopback, pin the SSH host key, use a dedicated restricted key and supervise reconnection. The repository contains no personal tunnel keys or host configuration.
