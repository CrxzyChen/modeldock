# ModelDock

**Self-hosted AI model deployment and media generation.**

ModelDock brings model installation, isolated deployment, GPU configuration and media generation into a desktop control panel. ModelDock Studio is the PC client; model inference runs on your Linux server.

> **Alpha release.** This repository is a sanitized source snapshot of MediaCenter 1.2.28. Internal Python modules, environment variables, protocol identifiers and application labels still use `mediacenter` / `MediaCenter`. They are retained to avoid silently changing runtime and stored-data contracts. Windows Studio is available as an unsigned installer; a ready-made public model-image registry is not included.

## Download

[Studio for Windows x64 and Server bundle](https://github.com/CrxzyChen/modeldock/releases/tag/v1.2.28-alpha.1) · [release notes](docs/RELEASE-1.2.28.md) · [Server installation guide](docs/DEPLOYMENT.md)

## What it does

- Manage image, video, speech and music model services.
- Install services using recipes and explicit runtime/model bindings.
- Configure GPU placement and on-demand, warm-idle or resident model loading.
- Import model assets, LoRA and VAE, subject to supported compatibility contracts.
- Keep model weights outside container images and retain assets separately from deployment instances.
- Use an Electron + Vue 3 desktop UI with authenticated HTTP APIs and SSE updates.

## Architecture

```text
ModelDock Studio (Electron / Vue / TypeScript)
             | HTTPS API + SSE
Linux control server / scheduler
             | Redis Streams: reliable commands and results
             | Redis Pub/Sub: lossy heartbeat and progress
Isolated model Workers / GPU resources
             | mounted model assets and output storage
```

SQLite stores control-plane state. Reliable Redis streams retain persistence; high-frequency telemetry uses bounded memory and is not a durable event log. Each model instance uses an explicit runtime image and asset binding.

## Desktop development

Install Node.js compatible with the pinned Vite/Electron toolchain and Python 3.10+ for server development.

```sh
npm ci
npm run check
npm start
```

The desktop connects to an independently configured server. It does not launch an inference server on your PC. Add your server URL and API Key in the client. Use HTTPS outside a trusted local network.

## Server and container deployment

Deployment requires a Linux host, explicitly configured GPUs, a supported container runtime, private Redis and operator-built/approved Worker images. Model access and license requirements depend on the chosen model.

Start with [deployment notes](docs/DEPLOYMENT.md), the templates in `deploy/`, and the fixed image build definitions in `containers/`. Historical image descriptors are build inputs, **not a hosted installation catalog**. A new environment needs its own verified artifacts and runtime configuration; copying a template alone does not install a working GPU service.

## Verification

```sh
python -m pip install redis==5.3.1 Pillow==11.3.0
python -m unittest tests.test_worker_protocol tests.test_redis_transport tests.test_worker_runtime
npm run check
```

Some integration tests require Linux, Redis or an isolated container engine and must not be pointed at production. Internal project-status/acceptance-board tests and their private evidence are intentionally excluded. See [publication scope](docs/PUBLICATION.md).

## Security and licensing

No model weights, private credentials, production configurations or user outputs are distributed here. See [SECURITY.md](SECURITY.md).

Original ModelDock code and documentation are licensed under [Apache-2.0](LICENSE). See [NOTICE](NOTICE) and [third-party boundaries](THIRD_PARTY.md). Third-party dependencies, upstream model configurations/tokenizers and model weights retain their respective licenses. Review model licenses and obtain required permissions before downloading or using them.
