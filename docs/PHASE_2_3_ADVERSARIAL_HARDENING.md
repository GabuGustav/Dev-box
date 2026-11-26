# Phase 2.3 — Adversarial Security Hardening

**Branch:** `phase-2-adversarial-hardening`  
**Base branch:** `phase-2-rootless-worker-hardening`  
**Base commit:** `2257a94e872266294400350233196ced562e4a43`  
**Inherited verification:** CI run [45870278489](https://github.com/GabuGustav/Dev-box/actions/runs/45870278489) — 174 passed, 7 skipped, 71% coverage; all 7 Linux XFS/Docker integration tests passed. The base branch received documentation-only commits after the tested code commit.

**Status:** Core hardening changes and regression tests implemented; Linux CI verification is pending. Snapshot fault-injection/fuzz coverage remains open.

## Objective

Close the remaining Phase 2 security gaps after the broker and rootless-worker migrations: untrusted Git repository configuration, adversarial snapshot archives and restore recovery, proxy destination validation, container runtime isolation evidence, and concurrency safety.

This phase does not claim hostile-tenant isolation. The worker still has group-based read/write access to all workspace and snapshot trees on the shared XFS mounts. A separate per-workspace storage boundary or microVM design remains a future architecture decision.

## Investigation findings

### 1. Git helper configuration

The broker runs a fixed set of Git commands in short-lived, unprivileged containers. The helper already disables system/global Git configuration, hooks, pagers, commit signing, external diff, and textconv; it also uses a fixed image, bounded output/time, and restricted network mode.

The helper still reads repository-local configuration. Config such as `core.fsmonitor` and `credential.helper` can invoke external programs. The fetch route resets the origin URL to a worker-validated URL, but local `url.*.insteadOf` rules and protocol configuration still need explicit defensive overrides. The helper container is sandboxed, but this is an unnecessary execution surface and can run while the egress proxy is available.

### 2. Snapshot archive extraction

Snapshot extraction is manual rather than using `TarFile.extractall()`. It already rejects absolute paths, parent traversal, duplicate normalized paths, special files, hard links, and escaping symlinks; it enforces archive size and entry limits, delays symlink creation until after regular entries, and verifies AES-GCM authentication.

The current tests cover baseline path and authentication checks, but review identified a concrete symlink-parent issue: an archived symlink can redirect the parent path used to create a later symlink. Each link target may pass its lexical check while the later link is actually created at a different resolved location. Reject any symlink entry nested beneath another archived symlink before creating any symlink. File/directory collisions, all special entry types, truncated archives, and every interrupted restore transition also need broader tests. Restore swaps are broker-owned and have a real XFS integration test; recovery idempotency and fault injection need broader coverage.

### 3. Egress proxy

Squid has an explicit domain allowlist, safe-port restrictions, CONNECT limited to 443, and a `dst` ACL denying common IPv4/IPv6 local, private, link-local, multicast, and special ranges. The `dst` ACL performs destination-address matching after DNS resolution.

The configured list does not explicitly cover all translation/tunneling ranges (including NAT64 and 6to4) or every IANA special-purpose range. There is no runtime test proving that an allowlisted hostname resolving to a private address is denied. Static config assertions alone are insufficient.

### 4. Container runtime profiles and user namespaces

Workspace and Git helper containers retain non-root users, dropped capabilities, read-only roots, no-new-privileges, private PID/IPC defaults, bounded resources, and no network by default (except Git helpers on the restricted proxy network).

Docker applies its default seccomp profile unless explicitly disabled; AppArmor's `docker-default` profile is applied when AppArmor is enabled. Verify active runtime protections rather than pinning a custom profile without compatibility evidence. Docker user-namespace remapping is not a safe one-line setting here: the current XFS bind-mount permission contract depends on host UID/GID ownership and needs an explicit subordinate-ID/idmapped-mount design first.

### 5. Concurrency

Workspace operations use process-local `threading.RLock` entries. The lock dictionary retains entries indefinitely, and the guarantee does not span multiple worker processes. Compose runs one worker by default, but scaling the service could silently invalidate serialization.

The broker and worker share persistent state volumes. Advisory file locks on those volumes can enforce one active broker and one active worker per Compose deployment; reference-counted workspace locks can remove idle entries. Multi-replica operation remains unsupported until a distributed lock design exists.

## Phased implementation plan

### 2.3.1 — Git helper hardening
- [x] Add command-line overrides disabling local fsmonitor execution, credential helpers, SSH command execution, hooks, submodule recursion, and unsafe protocols.
- [x] Set `GIT_ALLOW_PROTOCOL=https` and force the configured proxy for network-enabled operations.
- [x] Keep system/global Git configuration disabled and output/time/resource bounds intact.
- [x] Add tests for malicious fsmonitor configuration and `url.*.insteadOf` rewrites to local file transport.
- [x] Assert Git helper containers are non-privileged, have no devices, and mount only the selected workspace; Linux integration execution is pending.

### 2.3.2 — Snapshot archive adversarial testing
- [ ] Add a reusable encrypted-archive fixture or focused decrypted-tar test harness.
- [ ] Test normalized duplicate paths, file/directory collisions, symlink chains, symlink-parent paths (including a parent link that redirects a later link outside the stage), absolute paths, traversal, hard links, devices, FIFOs, and unsupported entry types.
- [ ] Reject symlink entries whose ancestor path is another symlink entry before creating any symlinks.
- [ ] Test oversized members, excessive entry counts, malformed/truncated tar headers, invalid PAX metadata, and authenticated-ciphertext tampering.
- [ ] Add fault injection at each restore transition: stage creation, extraction, old-tree backup, stage swap, registry identity refresh, quota reset, and cleanup.
- [ ] Prove recovery is idempotent and preserves either the previous complete tree or the restored complete tree; never expose a partial workspace.
- [ ] Add property-based tests if the existing dependency set supports them without a heavy runtime dependency.

### 2.3.3 — Egress proxy hardening
- [x] Expand blocked destinations to include IPv4 special-purpose ranges and IPv6 NAT64, local-use NAT64, IPv4-compatible/mapped, 6to4, Teredo/IETF protocol, documentation, discard-only, and other non-global ranges.
- [x] Add static checks for private/special destination ranges and a Linux proxy integration test with an allowlisted hostname resolving to private IPv4/IPv6.
- [x] Test unapproved domains and unsafe CONNECT ports in the proxy integration probe.
- [ ] Verify workspace containers and the worker cannot join/use the internal egress network directly.

### 2.3.4 — Container runtime and cross-workspace verification
- [x] Assert workspace HostConfig is non-privileged, has no devices, and does not request unconfined seccomp/AppArmor.
- [x] Verify the host reports seccomp and verify `docker-default` AppArmor when the host reports AppArmor support.
- [ ] Extend cross-workspace tests for bind-mount scope and symlink/relative-path escape attempts; existing PID/filesystem isolation tests remain enabled.
- [x] Record the userns-remap/subordinate-ID and XFS ownership incompatibility; defer enabling user namespaces until an explicit storage mapping design is tested.

### 2.3.5 — Concurrency and single-instance enforcement
- [x] Make workspace lock entries reference-counted and remove them when idle.
- [x] Add a non-blocking advisory lock in `broker_state` so only one broker can own the Docker/XFS control plane per deployment.
- [x] Add a non-blocking advisory lock in `worker_state` so only one worker process/container can operate against the shared worker state.
- [x] Set Uvicorn worker count explicitly to one.
- [x] Add unit tests for second-instance rejection, lock release, and unsafe lock paths; add workspace lock serialization/cleanup tests.
- [x] Keep multi-replica deployments explicitly unsupported until distributed locking is implemented.

### 2.3.6 — CI, documentation, and exit gate
- [ ] Run Ruff, unit tests, Compose validation, all image builds, and Linux Docker/XFS/proxy integration.
- [ ] Ensure CI reports all test counts, skips, and integration results.
- [x] Update `SECURITY.md`, `ARCHITECTURE.md`, `STORAGE.md`, `DEPLOYMENT.md`, `ROADMAP.md`, and this plan.
- [x] Record remaining risks accurately; do not claim hostile-tenant isolation.

## External references

- [Git configuration reference](https://git-scm.com/docs/git-config)
- [Git credential helpers](https://git-scm.com/docs/gitcredentials)
- [Squid ACL reference](https://www.squid-cache.org/Doc/config/acl/)
- [Docker seccomp profile](https://docs.docker.com/engine/security/seccomp/)
- [Docker AppArmor profile](https://docs.docker.com/engine/security/apparmor/)
- [Docker user namespace remapping](https://docs.docker.com/engine/security/userns-remap/)

## Progress log

### Investigation
- [x] Review Git helper command allowlist, local config handling, network mode, proxy environment, and container limits.
- [x] Review archive validation, manual extraction order, encrypted snapshot format, and restore broker transitions.
- [x] Review Squid ACLs, Compose network membership, and current proxy tests.
- [x] Review workspace/Git helper container security settings and Docker default-profile behavior.
- [x] Review process-local workspace locking and deployment replica assumptions.

### Implementation
- [x] Git helper hardening and malicious-configuration regression tests added.
- [x] Snapshot symlink-parent protection and baseline adversarial archive tests added; fault injection/fuzzing remain.
- [x] Proxy address-range expansion and DNS-to-private integration test added.
- [x] Container profile assertions added; cross-workspace integration coverage retained.
- [x] Single-instance locks and lock-registry cleanup implemented.
- [ ] Full CI verification and remaining restore fault-injection coverage.
