# Phase 2 Security Hardening Plan

**Investigation branch:** `phase-2-security-hardening`  
**Broker branch:** `phase-2-privileged-broker`  
**Current hardening branch:** `phase-2-rootless-worker-hardening`  
**Current base commit:** `088364d3bffd7fb03bc9010bd1be9057824bffb1`  
**Base CI:** [45868184349](https://github.com/GabuGustav/Dev-box/actions/runs/45868184349) passed.  
**Status:** Deployment hardening, broker migration, and rootless-worker storage hardening are verified. Adversarial archive, egress, cross-workspace, and concurrency work remains.

## Baseline findings and current disposition

1. PostgreSQL was published on host port 5432 with static `devbox/devbox` credentials. **Remediated:** PostgreSQL is internal-only and Compose requires deployment-specific `DB_PASSWORD`.
2. The API was published on host port 8000 over direct HTTP. **Remediated by default:** API binds to host loopback; external deployments still require TLS termination at a trusted reverse proxy.
3. The worker mounted the Docker socket and received `CAP_SYS_ADMIN`. **Remediated by broker migration:** the broker alone owns Docker socket and XFS authority. The current branch further separates worker UID/GID from workspace runtime and prevents worker writes to XFS mount roots.
4. Workspace containers have non-root execution, dropped capabilities, read-only roots, no-new-privileges, private PID/IPC, resource limits, and no network by default. Docker's default seccomp and AppArmor profiles are now asserted by Linux integration tests when the host supports them; hostile tenant-boundary verification remains incomplete.
5. The egress proxy now blocks additional IPv4/IPv6 translation ranges and has a DNS-to-private integration test. Runtime verification is pending on the current branch.
6. Workspace operations remain process-local, but advisory file locks now enforce one broker and one worker per deployment; both Uvicorn services are pinned to one process. Multi-replica support remains unsupported.
7. Snapshot review found a symlink-parent chain that could create an escaping symlink in the restore stage. Extraction now rejects archive entries nested beneath another symlink, with a regression test. More restore fault injection and fuzz coverage remain incomplete.
8. Dedicated Linux XFS/Docker integration exists and passed its five-test baseline. The current rootless-worker changes need a fresh run on the branch.

These findings distinguish confirmed unsafe defaults from risks that still require exploitability testing. No host escape or snapshot extraction exploit has been demonstrated by source review alone.

## Phases and subphases

### 2.1 — Deployment exposure and secret hygiene
- [x] Remove PostgreSQL host-port publishing.
- [x] Replace hard-coded DB credentials with required environment inputs and reject placeholders in production configuration.
- [x] Preserve localhost-only API binding by default.
- [x] Document TLS termination for non-local deployments.
- [x] Add Compose/configuration tests and verify in CI runs 45860830556 and 45868184349.

### 2.2 — Privileged broker and worker boundary

Detailed plans: [Phase 2.2 Privileged Broker](PHASE_2_2_PRIVILEGED_BROKER.md) and [Rootless Worker](PHASE_2_2_3_ROOTLESS_WORKER.md).

- [x] 2.2.1 Inventory privileged operations and define typed broker contracts.
- [x] 2.2.2 Add a private broker, separate service token, fixed Docker operations, and workspace-scoped XFS RPC.
- [x] 2.2.3 Move XFS mount/project/quota administration behind broker; worker no longer has `CAP_SYS_ADMIN`.
- [x] 2.2.4 Move Docker lifecycle, image builds, Git helper containers, and reconciliation behind broker; worker no longer mounts Docker socket or imports Docker SDK.
- [x] 2.2.5 Complete Git helper local-config guardrails, HTTPS-only transport, proxy enforcement, and malicious fsmonitor/file-transport regression tests (current-branch CI pending).
- [x] 2.2.6 Implement and verify dedicated worker UID/GID `10002:10001`, all-capability drop, `no-new-privileges`, read-only root, isolated worker state, non-writable XFS mount roots, and broker-owned restore swaps (CI run 45870278489 passed).
- [x] 2.2.7 Complete current-branch CI and release documentation.
- [ ] Complete adversarial Git/archive/egress/cross-workspace/concurrency tests.

**Verified broker evidence:** CI run [45864319247](https://github.com/GabuGustav/Dev-box/actions/runs/45864319247) passed 146 unit tests (five skipped in quality) and all five Linux XFS/Docker integration tests. The latest base CI run [45868184349](https://github.com/GabuGustav/Dev-box/actions/runs/45868184349) passed quality, image builds, Compose validation, and integration.

**Remaining trust boundary:** the worker still has group-based read/write access to all workspace and snapshot directories on shared XFS mounts. This is not hostile-tenant isolation.

### 2.3 — Workspace runtime isolation

Detailed implementation and status: [Phase 2.3 Adversarial Hardening](PHASE_2_3_ADVERSARIAL_HARDENING.md).
- [x] Verify Docker's default seccomp profile and `docker-default` AppArmor when host support is reported; reject unconfined container settings.
- [x] Record the userns-remap/subordinate-ID and XFS bind-mount ownership constraints; defer enabling remapping until a tested mapping design exists.
- [ ] Extend cross-workspace filesystem/process/device/mount tests beyond the existing filesystem and PID isolation suite.
- [ ] Evaluate microVMs against XFS quota requirements and document the trade-offs.

### 2.4 — Snapshot and storage verification
- [x] Add malicious archive regression tests for normalized duplicates, file-parent collisions, symlink-parent chains, hard links, devices, and FIFOs.
- [ ] Extend malicious archive tests to truncated/PAX metadata, expanded-size/entry-count boundaries, and property-based fuzzing.
- [ ] Add interrupted create/restore/delete and recovery-state fault injection.
- [ ] Test concurrent restore/delete and workspace lifecycle interactions.
- [x] Run real XFS quota and Docker persistence/isolation tests on dedicated Linux infrastructure (seven integration tests passed in run 45870278489).
- [ ] Document and rehearse backup/restore, registry recovery, and encryption-key recovery.

### 2.5 — Network and concurrency controls
- [x] Expand Squid destination blocks for NAT64, local-use NAT64, 6to4, Teredo/IETF protocol, documentation, discard-only, and other special ranges.
- [x] Add a proxy integration test with an allowlisted hostname resolving to private IPv4/IPv6; also probe unapproved domains and unsafe CONNECT ports.
- [ ] Add dynamic DNS-rebinding/redirect tests and verify metadata endpoints across supported deployment networks.
- [ ] Add tests for worker crash, simultaneous lifecycle/snapshot/delete calls, and stale reconciliation.
- [x] Enforce one broker and one worker per deployment using persistent-volume advisory locks and one Uvicorn worker; multi-replica deployment remains unsupported.

### 2.6 — CI, documentation, and release gate
- [ ] Add security-focused CI jobs for static/config checks and adversarial unit tests.
- [x] Add Linux Docker/XFS integration workflow with disposable loopback XFS filesystems.
- [x] Make CI report integration tests as passed and report remaining skips with explicit reasons.
- [x] Update security, architecture, storage, deployment, and roadmap documents for the broker/rootless-worker design.
- [x] Update security, architecture, storage, deployment, and roadmap documents for Phase 2.3 controls.
- [ ] Run and inspect the complete current-branch test/lint/build suite.

## Execution rules

- Implement in order; do not start Phase 3 credential handling or Phase 4 arbitrary build execution until relevant Phase 2 security gates are met.
- For major architectural work, create a new branch from the latest verified successful branch.
- Keep development convenience separate from production defaults.
- Do not mark an item complete based on source edits alone: require tests and record their results.
- Update this document after each subphase with changed files, test evidence, risks, and remaining work.

## Progress log

### Investigation and deployment hardening
- [x] Inspected Compose deployment topology, API authentication/ownership, worker Docker/XFS privileges, workspace container settings, snapshot/storage implementation, egress proxy, lifecycle locking, and CI workflow.
- [x] Completed deployment exposure and secret-hygiene remediation; CI runs 45860830556 and 45868184349 passed.
- [x] Verified Docker/XFS broker migration in CI run 45864319247.

### Rootless-worker branch
- [x] Created `phase-2-rootless-worker-hardening` from the latest successful `phase-2-privileged-broker` commit `088364d3`.
- [x] Added separate worker identity, broker-owned XFS mount-root permissions, worker-state volume, snapshot metadata migration, and broker-owned restore transitions.
- [x] Added unit and Linux integration tests for rootless access and restore semantics.
- [x] Updated implementation and security documentation.
- [x] Verify all current-branch changes in GitHub Actions; quality and Linux XFS/Docker jobs both passed in run 45870278489.

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
