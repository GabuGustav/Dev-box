# Phase 2 Security Hardening Plan

**Branch:** `phase-2-security-hardening`  
**Base:** `phase-2-isolated-workspaces` (branch ref at branch creation)  
**Status:** Investigation complete; remediation not yet verified  
**Scope:** Close deployment-default exposure, reduce privileged-worker blast radius, validate container and storage isolation, and make security checks repeatable in CI.

## Baseline findings

The source audit confirmed the following issues or verification gaps:

1. PostgreSQL is published on host port 5432 with static `devbox/devbox` credentials in Compose.
2. The API is published on host port 8000 and runs plain HTTP directly. This is acceptable only for a trusted local development deployment, not an internet-facing deployment.
3. The worker mounts the Docker socket and receives `CAP_SYS_ADMIN` to administer XFS quotas. A worker compromise can become host compromise. This is an architectural trust-boundary risk, not a demonstrated remote exploit.
4. Workspace containers have meaningful hardening (non-root UID, dropped capabilities, read-only root filesystem, no-new-privileges, private PID/IPC, resource limits, and no network by default), but explicit runtime-profile and tenant-boundary adversarial verification is incomplete.
5. Egress proxy protections exist, but DNS rebinding, IPv4/IPv6 private-address bypass, and proxy bypass require adversarial tests.
6. Per-workspace operation locks are in-process only; multiple worker processes/replicas are not safe without distributed coordination.
7. Snapshot code has encryption and archive validation controls, but real XFS/Docker recovery drills and adversarial archive tests remain incomplete.
8. The existing CI workflow runs quality/unit tests but does not provide a dedicated security integration lane. The five Linux/XFS/Docker-dependent tests were previously reported skipped; this must be verified on a capable runner.

These findings distinguish confirmed unsafe defaults from risks that still require exploitability testing. No host escape or snapshot extraction exploit has been demonstrated by source review alone.

## Phases and subphases

### 2.1 — Deployment exposure and secret hygiene
- [x] 2.1.1 Remove PostgreSQL host-port publishing; keep DB reachable only on the Compose service network.
- [x] 2.1.2 Replace hard-coded DB credentials with required environment inputs and reject known placeholder values in production configuration.
- [x] 2.1.3 Preserve a documented local-development path with localhost-only API binding by default.
- [x] 2.1.4 Document TLS termination at a reverse proxy for non-local deployments.
- [ ] 2.1.5 Add Compose/configuration tests for secure defaults and missing/placeholder secrets (configuration tests added; first CI run caught one Ruff line-length failure, fixed in the branch; a fresh run is still required).

**Exit:** no public database listener by default; no committed production credentials; secure deployment requirements are executable and documented.

### 2.2 — Privileged worker boundary
- [ ] 2.2.1 Map every operation requiring Docker API access and XFS administration.
- [ ] 2.2.2 Design a narrow privileged host broker for the minimum Docker/XFS operations, with typed requests, strict workspace/path validation, and no arbitrary Docker API passthrough.
- [ ] 2.2.3 Separate unprivileged worker orchestration from the privileged broker.
- [ ] 2.2.4 Authenticate broker calls, restrict its network reachability, and add audit logging.
- [ ] 2.2.5 Add negative tests proving the broker cannot be used for arbitrary container creation, host path access, or cross-workspace operations.

**Exit:** general worker process no longer has direct Docker socket access or `CAP_SYS_ADMIN`; broker API is narrowly scoped and tested. Do not implement until the broker contract and operational failure modes are reviewed.

### 2.3 — Workspace runtime isolation
- [ ] 2.3.1 Select and pin explicit seccomp and AppArmor/SELinux policy where supported; retain safe runtime defaults.
- [ ] 2.3.2 Evaluate Docker user namespaces/idmapped mounts and per-workspace host identity separation.
- [ ] 2.3.3 Add adversarial cross-workspace filesystem/process/device/mount tests.
- [ ] 2.3.4 Evaluate rootless execution and microVMs against the XFS quota requirements; document trade-offs rather than claiming container isolation is a VM boundary.

**Exit:** runtime policy is explicit, tested on supported Linux hosts, and limitations are documented.

### 2.4 — Snapshot and storage verification
- [ ] 2.4.1 Add malicious archive tests: traversal, absolute paths, duplicate names, symlink chains, hard links, special files, and oversized archives.
- [ ] 2.4.2 Add interrupted create/restore/delete and recovery-state tests.
- [ ] 2.4.3 Test concurrent restore/delete and workspace lifecycle interactions.
- [ ] 2.4.4 Run real XFS quota and Docker persistence/isolation tests on dedicated Linux infrastructure.
- [ ] 2.4.5 Document and rehearse backup/restore, registry recovery, and encryption-key recovery.

**Exit:** hostile archive tests pass and an actual Linux XFS/Docker recovery drill is recorded.

### 2.5 — Network and concurrency controls
- [ ] 2.5.1 Test proxy bypass, DNS rebinding, redirects, IPv4/IPv6 private/link-local/loopback destinations, and metadata endpoints.
- [ ] 2.5.2 Harden proxy destination resolution and connection-time IP validation where tests expose gaps.
- [ ] 2.5.3 Add tests for worker crash, simultaneous lifecycle/snapshot/delete calls, and stale reconciliation.
- [ ] 2.5.4 Replace process-local locks before enabling multiple worker processes or replicas; otherwise enforce and document a single-process deployment invariant.

**Exit:** egress bypass tests pass; concurrency model is safe for the declared deployment topology.

### 2.6 — CI, documentation, and release gate
- [ ] 2.6.1 Add a security-focused CI job for static/config checks and adversarial unit tests.
- [ ] 2.6.2 Add a Linux Docker/XFS integration workflow on a runner that can safely provision dedicated loopback XFS filesystems; never run destructive tests against the runner root filesystem.
- [ ] 2.6.3 Make CI report integration tests as passed, skipped with an explicit reason, or unavailable; do not silently count skipped tests as full verification.
- [ ] 2.6.4 Update `SECURITY.md`, `ARCHITECTURE.md`, `STORAGE.md`, and `ROADMAP.md` as implementation decisions change.
- [ ] 2.6.5 Run the complete test/lint/build suite and review the resulting Actions run.

**Exit:** all required unit/security tests pass; integration verification is green on supported Linux infrastructure; documentation matches the implemented design.

## Execution rules

- Implement in order; do not start Phase 3 credential handling or Phase 4 arbitrary build execution until relevant Phase 2 security gates are met.
- For major architectural work, create a new branch from the latest verified successful branch.
- Keep development convenience separate from production defaults.
- Do not mark an item complete based on source edits alone: require tests and record their results.
- Update this document after each subphase with changed files, test evidence, risks, and remaining work.

## Progress log

### Investigation
- [x] Inspected Compose deployment topology, API authentication/ownership, worker Docker/XFS privileges, workspace container settings, snapshot/storage implementation, egress proxy, lifecycle locking, and CI workflow.
- [x] Created this remediation branch from `phase-2-isolated-workspaces`.
- [x] Confirm the remediation branch was created from the Phase 2 branch ref; exact base SHA is recorded in PR #1.
- [ ] Re-verify a green CI run after the lint correction before treating Phase 2.1 as verified.
- [x] Phase 2.1 implementation edits committed: Compose, environment template, production settings validation, tests, CI trigger/Compose validation, and deployment documentation. Test results are pending; do not treat Phase 2.1 as complete until Actions is green.


## Privileged broker design constraints (research outcome)

Docker's own security guidance explains that daemon control can mount arbitrary host paths into containers and can therefore alter the host filesystem. A generic TCP Docker API or a read-only socket mount is not a meaningful privilege boundary. The implementation must be a typed host capability broker, not a Docker socket proxy.

### Required topology

```text
API/control plane
      |
      | authenticated, workspace-scoped operation
      v
Unprivileged worker (no Docker socket, no CAP_SYS_ADMIN)
      |
      | private broker protocol; no public route
      v
Privileged host broker (Docker socket + narrowly required XFS authority)
      |
      +-- fixed workspace container lifecycle
      +-- fixed Git-helper container lifecycle
      +-- XFS project quota operations
      +-- managed-container reconciliation
```

### Broker contract

- Accept a workspace ID matching the existing strict identifier format; derive all host paths, project IDs, container names, labels, images, and mount targets internally.
- Expose only named operations (ensure/start/stop/pause/resume/delete/inspect workspace, run an allowlisted Git helper operation, reconcile managed containers, inspect/assign/set/clear XFS project quotas).
- Never accept arbitrary Docker API JSON, image names, host paths, mount definitions, capabilities, devices, network names, entrypoints, or shell commands.
- Workspace containers must retain the current fixed hardening profile. Git helpers may use only the fixed Git image, the selected workspace mount, and the allowlisted egress network when the operation requires it.
- Quota operations must accept typed block/inode values and derive project IDs/paths from trusted registry state. The broker must not accept raw `xfs_quota` command strings.
- Authenticate broker requests with a separate secret, constant-time comparison, request-size limits, and private-network reachability. Keep the broker off public and egress networks.
- Log actor/service, workspace, operation, policy decision, and result without logging secrets or repository contents.
- Fail closed on missing registry entries, path identity mismatch, unknown operations, or invalid state.
- Add negative tests for arbitrary mounts, host-root paths, alternate images, devices, privileged flags, cross-workspace IDs, and arbitrary daemon API forwarding.

### Explicit limitation

This design reduces the worker's direct host-root capability; it does **not** by itself create a per-tenant trust boundary if the unprivileged worker can still read/write the shared workspace and snapshot roots or can request operations for arbitrary workspace IDs. Strong hostile-tenant isolation requires workspace-scoped authorization at the broker boundary and storage access scoped to one workspace operation (or a stronger VM/microVM architecture). Do not label the broker milestone complete until that limitation is either addressed or explicitly accepted in the threat model.

### Research references

- [Docker Engine security](https://docs.docker.com/engine/security/) — daemon access can expose host filesystems; Docker specifically warns about services that provision containers through a daemon API.
- [Docker daemon socket protection](https://docs.docker.com/engine/security/protect-access/) — prefer protected Unix-socket, SSH, or mutually authenticated TLS access; do not expose unauthenticated Docker TCP.
- [OWASP Docker Security Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Docker_Security_Cheat_Sheet.html) — socket access is effectively host-root access and a read-only mount does not make Docker API operations read-only.
- [xfs_quota(8)](https://man7.org/linux/man-pages/man8/xfs_quota.8.html) — XFS project-tree setup, clearing, and quota limit semantics.
