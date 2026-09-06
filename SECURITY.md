# Security

This Alpha repository manages privileged model workloads. Do not expose Redis, a container-engine socket, build daemons or Worker control endpoints publicly.

- Use HTTPS for remote clients and unique, strong API Keys.
- Keep API Keys, SSH keys, source-download tokens and actual runtime configuration outside Git.
- Mount weights and user data separately from images; do not distribute licensed weights with the application.
- Review GPU/cgroup/container isolation and resource limits for your own host.
- Never use example addresses or historical image metadata as a production trust decision.
- Do not run recovery or migration scripts against production without a verified backup and a reviewed plan.
- Do not include credentials, prompts, generated media or raw private logs in public issues.

Report vulnerabilities using GitHub private vulnerability reporting when enabled. Do not post exploit details or secrets to public issues.
