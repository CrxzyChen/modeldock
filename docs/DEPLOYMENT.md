# Deployment notes (Alpha)

The PC client and Linux server are separate. The server schedules independent model containers and owns model assets, tasks and outputs.

## Prerequisites

- Linux with a supported GPU driver and container execution environment.
- Python 3.10+ and the control dependencies pinned in `requirements/control-runtime.lock`.
- Private Redis with the limits and ACL scheme in `deploy/redis.conf` and `deploy/redis-acl.template`.
- Explicit GPU index/UUID configuration; no GPUs are implicitly reserved for another product.
- Operator-built and verified OCI images plus a runtime configuration referring to those exact artifacts.

## Entry points

`scripts/build_server_release.py --help` builds the source-based server bundle. `scripts/install_server_release.py --help` documents install/upgrade operations and their preflight controls. `deploy/mediacenter.env.example` is a substitution template, not an immediately runnable configuration.

Review `scripts/build_runtime_image.py`, `scripts/build_derived_oci_image.py` and `deploy/production-images/` for fixed runtime construction. Historical release descriptions must be reviewed and regenerated for your own source/build. They do not supply a current production image catalog. Do not weaken integrity checks to make an old image descriptor pass.

The server entry point is `python -m mediacenter.server`. The public health endpoint is `/healthz`; authenticated routes are under `/api/v1/`. Real service availability requires the full runtime configuration, not just starting the HTTP process.

## Public access

Put a TLS reverse proxy in front of the server. `deploy/nginx-mediacenter-sse.conf` documents SSE handling. Keep buffering/caching disabled for `/api/v1/events`. Set upload limits according to the actual client transfer protocol and your capacity.

For an internal server behind NAT, an operator may establish a restricted reverse SSH tunnel to a public proxy. Keep the reverse listener bound to loopback, pin the SSH host key, use a dedicated restricted key and supervise reconnection. The repository contains no personal tunnel keys or host configuration.
