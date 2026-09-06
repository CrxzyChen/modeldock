# Studio Alpha publication contract

- ID: PUB-002 — Studio binary and Server installation guide.
- Status: approved by user (request: release Studio and update Server installation plan).
- Paths: public-copy docs, README, and ignored build outputs only.
- Outcome: downloadable Windows installer, validated control-server bundle and accurate Linux instructions.
- Checks: client typecheck/tests/build, Electron renderer smoke, server bundle integrity/tests, packaged source allowlist, public asset SHA-256.
- Non-goals: UI rebranding, changing app identity, licensing decisions, live deployment, installing on the user's PC, publishing model weights or private OCI images.
- Stop conditions: secret exposure, broken packaging, missing artifact integrity or a requirement to mutate live data.

This publication does not close or accept the private PH-8 milestone.

## Verification result

- [x] Client typecheck, 92 tests and Windows x64 build.
- [x] Server bundle integrity, 14 release tests.
- [x] Packaged resource allowlist and source match; unsigned status recorded.
- [x] Read-only installation-guide review; missing FFmpeg prerequisite corrected.
- [ ] Full Electron smoke: model settings footer overflows approximately 10 CSS pixels in the tested desktop viewport. Disclosed in Alpha notes, not waived as passing.
- [ ] Clean-machine installer and fresh-host Linux/GPU acceptance (not performed).

Decision: publish an explicitly marked prerelease with the known UI limitation; do not mark a stable release or PH-8 acceptance.
