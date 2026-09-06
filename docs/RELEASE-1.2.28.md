# ModelDock Studio 1.2.28 Alpha

## Downloads

- `ModelDock-Studio-Setup-1.2.28.exe`: Windows x64 Studio installer.
- `MediaCenter-server-1.2.28.tar.gz`: Linux control-server bundle (retains the installer contract name; not a GPU runtime image).
- `SHA256SUMS.txt`: checksums for both files.

Download from [this release](https://github.com/CrxzyChen/modeldock/releases/tag/v1.2.28-alpha.1).

## Studio

Run the installer and connect with your own server URL and API Key. The installer does not install a local inference server, include model weights, or contain saved server connections. HTTPS is recommended for public access.

This Alpha retains the **MediaCenter** application name, application ID and user-data location. Existing installations are therefore not a separate side-by-side application. Back up your user profile before upgrading; do not uninstall or remove application data to upgrade. The UI has not been rebranded in this release.

This is an unsigned Alpha build. Verify the checksum and source before deciding whether to run it; do not disable system protections. SHA-256 verifies file integrity, not publisher identity. Clean-machine installation and full Linux/GPU acceptance are not claimed by this release.

## Server

Follow [the Linux installation and upgrade guide](DEPLOYMENT.md). The bundle installs versioned control-plane files; private Redis, the container engine, GPU runtime artifacts and runtime configuration must be prepared separately. There is no turnkey public Worker-image registry yet.

## Verification and known limitations

- Client typecheck and all 92 unit tests pass; Windows x64 packaging completes.
- Server bundle integrity validation and 14 release/installer tests pass.
- Electron smoke reaches the overview, model center, import wizard, image, status, service, hardware, video and speech views. The full smoke suite does **not** pass: the model settings footer extends about 10 pixels beyond the viewport in the tested Windows desktop setup (1426 CSS-pixel viewport). This UI layout issue remains open in this Alpha. Compact/zoom and later smoke checks are not claimed as passed.
- Packaged application resources were checked against the sanitized source allowlist. No clean-machine installer acceptance or production deployment was performed for this public build.

## Publication scope

Based on the sanitized 1.2.28 source. Includes model service management, residency configuration, model/LoRA/VAE import interfaces and media workspaces, subject to server-side runtime support. No private deployment history, personal addresses, API keys, model weights or user outputs are included. No existing server is modified by publishing this release.

The project license has not yet been selected. Third-party components retain their own licenses; the packaged Electron license notices are included in the application distribution.
