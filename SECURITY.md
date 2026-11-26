# DevBox — Security and Isolation

## 1. Security objective

DevBox executes code from repositories and commands requested by AI agents. Treat repository content, build scripts, dependencies, tool output, and agent-generated commands as untrusted. A compromised workspace must not compromise another workspace, the control plane, host infrastructure, or user credentials.

## 2. Trust boundaries

- User/browser to API.
- AI agent to MCP/API.
- Control plane to worker supervisor.
- Worker supervisor to execution container or VM.
- One workspace to another workspace.
- Workspace to host, internal network, and external network.
- Workspace to Git provider and credential store.

Every boundary requires authentication, authorization, and validation.

## 3. Workspace isolation

Default policy:
- One isolated execution boundary and filesystem per workspace.
- Separate process, mount, user, IPC, and network namespaces where supported.
- Run as non-root inside the workspace.
- Drop unnecessary Linux capabilities.
- Apply seccomp and mandatory access controls (AppArmor/SELinux) where available.
- Use read-only base images and a dedicated writable workspace directory with filesystem-enforced project quotas.
- Do not mount host root, sensitive host directories, or the container runtime socket.
- Do not share writable caches between tenants unless the cache is designed for safe, content-addressed sharing.
- Enforce CPU, memory, disk block and inode, process, file descriptor, and wall-clock quotas.
- Kill descendant processes and clean temporary resources on cancellation or deletion.

The privileged broker has Docker socket access and CAP_SYS_ADMIN to administer XFS quotas; compromise of the broker can compromise the host. Keep it private and treat it as a privileged infrastructure component. The orchestration worker runs as UID/GID 10002:10001, drops all Linux capabilities, uses a read-only root filesystem, and reaches the broker only over a private authenticated network. It still has group-based read/write access to workspace and snapshot trees on shared mounts, so a compromised worker can access multiple tenants' data. This is a blast-radius reduction, not hostile-tenant isolation. Containers reduce risk but share the host kernel. For hostile multi-tenant workloads, evaluate microVMs or dedicated VMs as a stronger boundary.

## 4. Identity and authorization

- Authenticate humans and agents separately.
- Workspace IDs are identifiers, never bearer secrets.
- Authorize every read, write, command, job, log, artifact, Git, and sharing request.
- Enforce ownership and grants on the server; never trust client-provided owner or role fields.
- Private workspaces are private by default.
- Grants are scoped to one workspace and one principal.
- Initial roles: `read`, `write`, `admin`.
- Only the owner or an authorized administrator may grant access.
- Sharing is non-transitive by default.
- Revocation must block new operations immediately and invalidate active sessions/credentials as appropriate.
- Record grant, use, and revocation events.

## 5. Command execution

- Treat command strings and repository scripts as untrusted input.
- Execute only inside the assigned workspace boundary.
- Apply timeouts, output limits, resource quotas, and cancellation.
- Do not execute commands through a shell in the control-plane process.
- Prefer structured command arguments and configured build profiles over unrestricted shell access.
- If interactive shell access is enabled, it remains confined to the workspace and subject to the same quotas.
- Prevent path traversal and symlink escapes when reading/writing files.
- Validate artifact paths and prevent access outside approved workspace mounts.

## 6. Network policy

Default to restricted egress. Allow only the network access needed for configured dependency registries and source-control operations. Deny access to:
- Cloud instance metadata endpoints.
- Host and worker management interfaces.
- Private/internal control-plane networks.
- Other workspace endpoints.
- Localhost services outside the workspace boundary.

Apply DNS and IP-level controls to reduce DNS rebinding and SSRF risks. Network allowlists must be configurable per workload class.

The initial controlled-egress foundation uses a dedicated internal-only Docker network for outbound helper containers. A private Squid proxy is the only service attached to both that internal network and the external bridge; it is not published on a host port. The proxy allows only approved Git hosting and package domains, denies private, loopback, link-local, and other local destinations, and denies all unlisted domains. Ordinary workspace containers remain on `network_mode=none`; only explicitly managed helper containers should join the restricted egress network. This proxy foundation does not yet mean repository cloning or arbitrary workspace internet access is enabled.

## 7. Secrets and credentials

- Store provider tokens in an encrypted credential store or secret manager.
- Use least-privilege, repository-scoped, short-lived credentials.
- Inject secrets only for the operation that needs them.
- Never intentionally write credentials into repository files, images, command history, logs, artifacts, or workspace snapshots.
- Redact secrets from stdout/stderr and error messages.
- Do not expose GitHub write credentials to read-only agents.
- Revoke credentials when access is revoked or a workspace is deleted.

## 8. Git safety

- A public repository URL grants no write access.
- Verify Git provider identity and repository permissions.
- Use branches for isolated agent work.
- Never force-push or overwrite remote history by default.
- Require explicit authorization for push, merge, release, or publication.
- Detect dirty working trees and conflicts before destructive operations.
- Keep an audit record of commits and remote changes.

## 9. Persistence, snapshots, and deletion

- Encrypt data in transit and at rest.
- Restrict snapshots and backups to authorized workspace owners and trusted services.
- Encrypt snapshot archives with authenticated encryption (AES-256-GCM) and store them on a separate quota-limited filesystem.
- Keep the snapshot encryption key outside the snapshot filesystem and back it up securely; key loss makes existing snapshots unrecoverable.
- Treat snapshot contents as sensitive: archives include workspace files as-is, including credentials accidentally written into the workspace. Encryption protects data at rest but does not remove secrets from the archive.
- Validate archive paths and symbolic links before restore; reject traversal and unsupported filesystem objects.
- Define retention and deletion behaviour for workspace data, logs, artifacts, and backups.
- Deletion should revoke credentials and grants, terminate executors, and schedule durable data removal according to the published retention policy.
- Test restore procedures and access controls.
- Delete a workspace's snapshots when its workspace data is permanently deleted, subject to the documented retention policy.

## 10. Audit and incident response

Record actor, workspace, operation, target, timestamp, request/job ID, result, and relevant policy decision. Avoid recording secrets or unnecessary source contents.

Provide:
- Centralized security and execution logs.
- Alerts for repeated authorization failures, resource abuse, and suspicious network activity.
- Ability to suspend an agent, workspace, or credential.
- Incident procedures for suspected container escape, credential exposure, and cross-workspace access.

## 11. Minimum security tests

- Agent A cannot list, read, write, or execute in Agent B's workspace.
- Guessing a valid workspace ID does not bypass authorization.
- A revoked grant no longer authorizes access.
- A workspace cannot read host files or another workspace's volume or processes.
- A job cannot exceed configured resource or time limits.
- A job cannot access cloud metadata or internal management endpoints.
- Malicious filenames, symlinks, and archive entries cannot escape the workspace.
- Operation-scoped provider credentials do not persist into workspace files or snapshots; credentials already written into a workspace are treated as sensitive encrypted snapshot content and must be rotated or removed.
- Cancellation terminates child processes and releases resources.


## 12. Phase 2 audit status (2026-11-26)

The reviewed implementation has useful baseline controls but is **not production-ready for hostile multi-tenant execution**. Confirmed deployment-default problems have been corrected on the security-hardening branch. The current work reduces the worker's ambient privileges but does not yet provide per-tenant storage isolation.

### Deployment exposure — remediated in the current branch lineage

- PostgreSQL no longer publishes a host port and uses a required deployment-specific `DB_PASSWORD`.
- The API binds to host loopback by default. Plain HTTP is permitted only behind a trusted local/reverse-proxy network; external deployments require TLS termination.
- Compose does not publish worker or broker ports.

### Privileged execution boundary — brokered, rootless worker transition in progress

- The Docker socket, Docker SDK, and XFS quota commands are broker-owned. The worker has no Docker socket and no `CAP_SYS_ADMIN`.
- The broker is the only service with Docker socket access and `CAP_SYS_ADMIN`; it is host-privileged and must remain private.
- The current branch configures the worker as UID/GID `10002:10001`, drops all capabilities, enables `no-new-privileges`, and makes its root filesystem read-only. The worker state volume is separate from broker state and XFS mount roots.
- The worker retains group-based read/write access to all workspace and snapshot trees because snapshot encryption and Git operations still run in the worker. A compromised worker can therefore access multiple tenants' data. Do not describe this as hostile-tenant isolation.
- Snapshot directory swaps and interrupted-restore recovery are delegated to typed broker operations so the worker does not need write access to the XFS mount root.

The rootless-worker code and tests are committed on `phase-2-xfs-quota-broker`; CI verification is pending. The parent branch `phase-2-privileged-broker` passed run [45864319247](https://github.com/GabuGustav/Dev-box/actions/runs/45864319247): 146 unit tests passed, five were skipped in the quality job, and all five dedicated Linux XFS/Docker integration tests passed.

### Controls observed in source

Workspace containers run as a non-root user, drop Linux capabilities, use a read-only root filesystem, enable `no-new-privileges`, use private PID/IPC namespaces, apply CPU/memory/PID limits, and default to no network. Snapshot code uses authenticated AES-GCM encryption and rejects unsafe archive entry types. API keys are stored as hashes and workspace ownership checks are present in the inspected routes.

These controls reduce risk but do not establish a VM-equivalent isolation boundary.

### Verification still required

- CI verification of the rootless worker, persistent metadata migration, broker restore transitions, and group-permission model.
- Adversarial container escape and cross-workspace tests on supported Linux hosts.
- Explicit seccomp/AppArmor/SELinux policy validation and user-namespace assessment.
- Proxy bypass, DNS rebinding, IPv4/IPv6, and metadata endpoint tests.
- Malicious snapshot archive, interrupted restore, and concurrent operation tests.
- Real XFS quota, Docker persistence, backup, and restore drills beyond the five-test baseline.
- Distributed locking before multiple worker processes or replicas are supported.

Track implementation and evidence in [docs/PHASE_2_SECURITY_HARDENING.md](docs/PHASE_2_SECURITY_HARDENING.md) and [docs/PHASE_2_2_3_ROOTLESS_WORKER.md](docs/PHASE_2_2_3_ROOTLESS_WORKER.md). Do not mark an item complete based on a documentation change or mocked test alone.

## 13. External security references

The remediation plan follows these primary references:

- [Docker Engine security](https://docs.docker.com/engine/security/) — Docker documents that daemon control can expose host filesystems and that a service provisioning containers through Docker must strictly validate every parameter.
- [Protect the Docker daemon socket](https://docs.docker.com/engine/security/protect-access/) — Docker documents authenticated SSH/TLS approaches for remote daemon access. DevBox should not replace the Unix socket with an unauthenticated TCP Docker API.
- [OWASP Docker Security Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Docker_Security_Cheat_Sheet.html) — reinforces that Docker socket access is effectively host-root access and that a read-only socket mount does not make the API read-only.
- [xfs_quota(8)](https://man7.org/linux/man-pages/man8/xfs_quota.8.html) — documents project-tree initialization and quota management operations used by the current storage backend.

The host broker is an application-level, typed capability boundary; it is not a generic Docker socket proxy.

## 14. Verification evidence

CI run [45860830556](https://github.com/GabuGustav/Dev-box/actions/runs/45860830556) verified the deployment-default changes and baseline Linux XFS/Docker integration. CI run [45864319247](https://github.com/GabuGustav/Dev-box/actions/runs/45864319247) verified the broker Docker migration, all image builds, Compose validation, Ruff, 146 passing unit tests, and all five Linux XFS/Docker integration tests. Neither run verifies the new rootless-worker changes on this branch.

