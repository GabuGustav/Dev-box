# Phase 2 Security Hardening Plan

**Branch:** `phase-2-security-hardening`  
**Base:** `phase-2-isolated-workspaces` (branch ref at branch creation)  
**Status:** Deployment hardening and broker migration implemented; rootless-worker verification in progress on `phase-2-xfs-quota-broker`  
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
- [x] 2.1.5 Add Compose/configuration tests for secure defaults and missing/placeholder secrets (108 tests passed in run 45860295750; the subsequent internal-proxy-network assertion is awaiting the next run).

**Exit:** no public database listener by default; no committed production credentials; secure deployment requirements are executable and documented.

### 2.2 — Privileged worker boundary

Detailed implementation plans: [Phase 2.2 Privileged Broker](PHASE_2_2_PRIVILEGED_BROKER.md) and [Phase 2.2.3 Rootless Worker](PHASE_2_2_3_ROOTLESS_WORKER.md). Work is based on the latest successful broker branch and continues on `phase-2-xfs-quota-broker`.

- [x] 2.2.1 Inventory Docker and XFS operations and define the typed broker contract.
- [x] 2.2.2 Add a private broker service, separate bearer token, fixed Docker operations, and workspace-scoped XFS quota RPC. Broker request-size limits and security audit events remain follow-up items.
- [x] 2.2.3 Move `findmnt`, XFS project registry, and `xfs_quota` operations behind broker methods; the worker has no `CAP_SYS_ADMIN`.
- [x] 2.2.4 Move Docker lifecycle, image build, Git helper containers, and reconciliation behind the broker; the worker has no Docker socket or Docker SDK.
- [ ] 2.2.5 Complete hostile Git configuration, timeout/output, and arbitrary helper capability tests.
- [x] 2.2.6 Implement worker UID/GID `10002:10001`, all-capability drop, `no-new-privileges`, read-only root, separate worker state, and broker-owned restore swaps. CI verification of the latest rootless changes is pending.
- [ ] 2.2.7 Complete current-branch CI and Linux XFS/Docker verification, then close documentation and release gates.

**Verified parent evidence:** CI run [45864319247](https://github.com/GabuGustav/Dev-box/actions/runs/45864319247) passed both quality and Linux XFS/Docker integration jobs (146 unit tests passed, five skipped in quality, all five integration tests passed). This verifies the Docker/XFS broker migration, not the subsequent rootless-worker commits.

**Remaining trust boundary:** worker and workspace files are still on shared XFS mounts. The worker's group `10001` grants access to all workspace and snapshot trees. A compromised worker can still read/write multiple tenants' data. The broker reduces host privilege but does not make the current design safe for hostile multi-tenancy.

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
- [x] 2.4.4 Run real XFS quota and Docker persistence/isolation tests on dedicated Linux infrastructure (five integration tests passed in CI run 45860830556).
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
- [x] 2.6.2 Add a Linux Docker/XFS integration workflow that provisions disposable loopback XFS filesystems; its first execution is pending.
- [x] 2.6.3 Make CI report integration tests as passed and report remaining skips with explicit reasons.
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
- [x] Re-verify deployment hardening and baseline XFS/Docker CI (runs 45860830556 and 45864319247 passed).
- [x] Phase 2.1 implementation and verification complete: Compose, environment template, production settings validation, tests, CI trigger/Compose validation, and deployment documentation.


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


**Broker migration checkpoint:** CI run [45864319247](https://github.com/GabuGustav/Dev-box/actions/runs/45864319247) passed (146 passed, five skipped, 71% coverage; all five Linux XFS/Docker integration tests passed). Docker lifecycle and Git operations are brokered; XFS operations are the remaining privilege blocker.


### Rootless worker implementation branch — `phase-2-xfs-quota-broker`

- [x] Created a new branch from the latest successful `phase-2-privileged-broker` line.
- [x] Added a dedicated worker-state volume, broker-owned mount-root permissions, group-based workspace directories, and snapshot metadata migration.
- [x] Delegated restore directory swaps and recovery to typed broker methods.
- [x] Configured the worker to run as UID/GID `10002:10001`, drop all capabilities, use a read-only root, and mount only bounded temporary/persistent writable state plus the workspace/snapshot data mounts.
- [x] Added unit tests and updated implementation/security documentation.
- [ ] Run and inspect CI on this branch; quality and Linux XFS/Docker integration must both pass before claiming this subphase complete.
