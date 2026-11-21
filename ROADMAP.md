# DevBox — Implementation Roadmap

This roadmap sequences the project so each phase produces a testable result. Estimates are intentionally omitted until the implementation stack and hosting budget are chosen.

## Phase 0 — Foundations

- [x] Define product principles and workspace isolation policy.
- [x] Define persistent workspace identity and repository discovery.
- [x] Define exclusive-by-default access with explicit sharing.
- [ ] Confirm initial deployment target and budget.
- [ ] Select implementation language/framework and supported worker OS.
- [ ] Define threat model and data retention policy.

**Exit criteria:** architecture and security decisions are sufficiently specific to implement.

## Phase 1 — Core platform skeleton

- [x] Create repository structure, backend application, configuration, and development environment.
- [x] Add database schema and migrations.
- [x] Add user and agent authentication (API-key principals).
- [x] Add workspace and repository metadata models.
- [x] Implement health checks, structured logging, and audit event foundation.
- [x] Add automated tests and CI.

**Exit criteria:** authenticated clients can create and list metadata-only workspaces. Implemented on `phase-1-core-platform`; CI passed for that branch.

## Phase 2 — Isolated persistent workspaces

- [x] Add a separate trusted worker supervisor; keep Docker socket access out of the API and workspace containers.
- [x] Provision one non-root container and persistent XFS-backed directory per workspace.
- [x] Add owner-scoped start, stop, pause, resume, and delete API operations.
- [x] Apply container CPU, memory, PID, capability, read-only-rootfs, and network-isolation settings.
- [x] Add database migration for executor and volume references.
- [x] Add mocked API lifecycle and cross-principal authorization tests.
- [x] Add executor unit tests for workspace ID scoping, token authentication, and hardened container settings.
- [x] Enforce per-workspace XFS hard block and inode quotas; fail closed on unsupported storage.
- [x] Implement encrypted snapshot create, list, restore, and delete with separate XFS quotas and archive path validation.
- [x] Reconcile active workspace lifecycle state from the trusted executor into PostgreSQL, with audit events and fail-safe behavior when the executor is unavailable.
- [ ] Verify workspace persistence and periodic worker recovery using Docker integration tests.
- [x] Add opt-in Docker integration tests for cross-workspace filesystem isolation, private PID namespaces, and persistence.
- [ ] Run adversarial host-escape and privilege-boundary testing.
- [ ] Add execution wall-clock limits with the job runner in Phase 4.

**Current Phase 2 boundary:** the executor creates persistent XFS-backed workspace directories with hard block and inode limits, applies container-level hardening, and supports encrypted snapshots on a separate quota-limited XFS mount. Workspaces have no network access yet, so repository cloning and dependency installation wait for controlled egress in Phase 3.

**Exit criteria:** not yet met. Snapshot implementation, periodic unsafe-mount enforcement, and API/database lifecycle reconciliation are implemented. Real XFS/Docker integration runs, backup/restore drills, and adversarial isolation testing are still required.

## Phase 3 — Git and repository discovery

- [x] Normalize repository URLs and store repository identities.
- [x] Implement owner-scoped repository workspace discovery by normalized URL.
- [ ] Clone repositories into workspaces.
- [ ] Track remote, branch, commit, and dirty working-tree state.
- [ ] Add branch, diff, commit, fetch, and push operations.
- [ ] Add safe conflict handling and pull-request workflow.
- [ ] Test reconnect from a completely new client session.

**Exit criteria:** a new chat/client can provide a repository URL, discover authorized workspaces, select one, and continue from its actual state.

## Phase 4 — Build and test engine

- [ ] Implement queued jobs and worker execution.
- [ ] Stream and persist logs.
- [ ] Capture exit codes, duration, and resource usage.
- [ ] Add cancellation, timeout, retry policy, and crash recovery.
- [ ] Add project build/test profiles.
- [ ] Store artifacts and associate them with jobs.
- [ ] Add static analysis and test result summaries.

**Exit criteria:** an agent can build and test a repository, inspect a failure, fix code, rerun, and compare results.

## Phase 5 — Agent interfaces

- [ ] Publish versioned REST API.
- [ ] Implement MCP server and documented tool schemas.
- [ ] Add API authentication and per-tool authorization.
- [ ] Add human web interface for workspace listing, logs, jobs, and permissions.
- [ ] Test at least two different agent clients against the same API.

**Exit criteria:** model-independent agents can operate DevBox without manual file copying.

## Phase 6 — Sharing and collaboration

- [ ] Implement read, write, and admin grants.
- [ ] Add grant expiry and revocation.
- [ ] Enforce permissions on every API and executor operation.
- [ ] Implement owner-controlled sharing and audit history.
- [ ] Add explicit Git-based change exchange and conflict review.
- [ ] Test revoked access and concurrent writes.

**Exit criteria:** workspace owners can safely grant and revoke scoped access without weakening isolation.

## Phase 7 — Hardening and production readiness

- [ ] Threat model review and security testing.
- [ ] Evaluate rootless containers versus microVM isolation.
- [ ] Add backup/restore drills and disaster recovery.
- [ ] Add quotas, billing/usage accounting if required, and abuse controls.
- [ ] Add metrics, alerting, retention, and operational runbooks.
- [ ] Load-test concurrent jobs and workspace recovery.

**Exit criteria:** documented operational and security requirements are met.

## Later capabilities

- Windows build workers for Windows-specific toolchains.
- GUI/desktop sessions and remote interaction.
- Optional GPU-backed runtime testing.
- Specialized build images for game modding, mobile, embedded, and other ecosystems.
- Multi-region scheduling and team administration.

## First implementation target

Do not begin with GPU virtualization, game launching, or a complex microservice fleet. Prove the core promise first:

**Two isolated agents → persistent workspaces → repository URL discovery → new-session reconnection → build/test → inspect and fix.**
