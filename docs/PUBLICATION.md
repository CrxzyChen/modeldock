# Public snapshot scope

This is the initial ModelDock Alpha source snapshot, derived from MediaCenter 1.2.28. It starts a new public Git history; it is not an export of internal deployment history.

Included: application source, tests, dependency locks, container build definitions and selected deployment templates.

Excluded from Git: live environment files, model weights, generated media, state databases, private acceptance evidence, project-control boards, deployment backups, user connection profiles, build outputs and installed dependencies. Five internal acceptance/project-status tests were excluded because their private evidence is not part of the public product source. The Studio Alpha installer and control-server bundle are distributed separately as release assets; see the release notes for verification limits.

Personal addresses and paths in source defaults/examples were replaced with loopback or generic paths. Internal module/protocol names remain unchanged. The runtime-v1 system-lock verification command records contain sanitized temporary paths; any published descriptor checksum for that edited lock is updated to the sanitized bytes. Historical build receipts are not proof of a new public image build.

The public snapshot was checked separately; original private project files and live services are not modified by publication.
