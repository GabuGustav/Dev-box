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

The trusted storage worker has Docker socket access and CAP_SYS_ADMIN to administer XFS quotas; compromise of that worker can compromise the host. Keep it private and treat it as a privileged control-plane component. Containers reduce risk but share the host kernel. For hostile multi-tenant workloads, evaluate microVMs or dedicated VMs as a stronger boundary.

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

The current implementation has useful baseline controls, but must not be described as production-ready for hostile multi-tenant execution.

### Confirmed deployment risks

- **Remediated:** PostgreSQL no longer publishes a host port and Compose requires a deployment-specific `DB_PASSWORD`.
- **Remediated by default:** the API binds to host loopback. External deployments still require TLS termination and a trusted-proxy configuration.
- The worker mounts the Docker socket and receives `CAP_SYS_ADMIN` for Docker/XFS operations. Compromise of this process can compromise the host. This is an elevated trust-boundary risk, not proof of a remotely exploitable path.

### Controls observed in source

Workspace containers run as a non-root user, drop Linux capabilities, use a read-only root filesystem, enable `no-new-privileges`, use private PID/IPC namespaces, apply CPU/memory/PID limits, and default to no network. Snapshot code uses authenticated AES-GCM encryption and rejects multiple unsafe archive entry types. API keys are stored as hashes and workspace ownership checks are present in the inspected routes.

These controls reduce risk but do not establish a VM-equivalent isolation boundary.

### Verification still required

- Adversarial container escape and cross-workspace tests on supported Linux hosts.
- Explicit seccomp/AppArmor/SELinux policy validation and user-namespace assessment.
- Proxy bypass, DNS rebinding, IPv4/IPv6, and metadata endpoint tests.
- Malicious snapshot archive, interrupted restore, and concurrent operation tests.
- Real XFS quota, Docker persistence, backup, and restore drills.
- Distributed locking before multiple worker processes or replicas are supported.

Track implementation and evidence in [docs/PHASE_2_SECURITY_HARDENING.md](docs/PHASE_2_SECURITY_HARDENING.md). Do not mark an item complete based on a documentation change or mocked test alone.


## 13. External security references

The remediation plan follows these primary references:

- [Docker Engine security](https://docs.docker.com/engine/security/) — Docker documents that daemon control can expose host filesystems and that a service provisioning containers through Docker must strictly validate every parameter.
- [Protect the Docker daemon socket](https://docs.docker.com/engine/security/protect-access/) — Docker documents authenticated SSH/TLS approaches for remote daemon access. DevBox should not replace the Unix socket with an unauthenticated TCP Docker API.
- [OWASP Docker Security Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Docker_Security_Cheat_Sheet.html) — reinforces that Docker socket access is effectively host-root access and that a read-only socket mount does not make the API read-only.
- [xfs_quota(8)](https://man7.org/linux/man-pages/man8/xfs_quota.8.html) — documents project-tree initialization and quota management operations used by the current storage backend.

The proposed host broker is therefore an application-level, typed capability boundary; it must not be a generic Docker socket proxy that forwards arbitrary daemon endpoints.


## 14. Remediation verification evidence

CI run [45860830556](https://github.com/GabuGustav/Dev-box/actions/runs/45860830556) passed on the security-hardening branch:

- Quality job: Compose validation and image builds passed; Ruff passed; 109 tests passed, 5 skipped; coverage 75%.
- Linux XFS/Docker integration job: 5 tests passed on disposable loopback XFS project-quota filesystems. This included block/inode quota enforcement, encrypted snapshot restore across separate XFS mounts, workspace mount isolation, persistence across container replacement, and private PID namespace isolation.

The integration run caught and led to correction of an invalid explicit Docker `pid_mode="private"` value. DevBox now relies on Docker's default private PID namespace, and the integration test verifies the isolation behavior.

This verifies the current baseline and Phase 2.1 deployment hardening; it does **not** complete the privileged-worker boundary, adversarial snapshot archive fuzzing, proxy bypass/DNS rebinding testing, distributed concurrency, or hostile-tenant isolation goals.


## 15. Privileged broker migration status

The `phase-2-privileged-broker` branch moved Docker lifecycle, image management, Git helper execution, reconciliation, and XFS quota administration behind typed broker operations. The worker no longer mounts `/var/run/docker.sock`, includes the Docker SDK, runs `xfs_quota`, or receives `CAP_SYS_ADMIN`. The broker is the only service with Docker daemon access and XFS authority.

CI run [45864319247](https://github.com/GabuGustav/Dev-box/actions/runs/45864319247) verified the Docker/XFS broker migration: 146 tests passed, five were skipped in the quality job, Ruff and image builds passed, and all five Linux XFS/Docker integration tests passed.

## 16. Rootless worker hardening — current branch

Branch `phase-2-rootless-worker-hardening` is based on successful commit `088364d3` (CI run [45868184349](https://github.com/GabuGustav/Dev-box/actions/runs/45868184349)).

The branch changes:
- Worker identity to UID/GID `10002:10001`; workspace runtime remains `10001:10001`.
- XFS mount roots to owner/group `0:10001`, mode `2750` (worker group can traverse/read but cannot create or rename mount-root entries).
- Per-workspace directories to owner/group `10001:10001`, mode `2770`.
- Snapshot metadata to a dedicated `worker_state` volume, with read-only migration from the legacy snapshot-root database.
- Snapshot restore directory commit and interrupted-restore recovery to broker RPC, including mount-root fsync.
- Worker container policy retains all-capability drop, `no-new-privileges`, read-only root, bounded tmpfs, and resource limits.

The rootless-worker code and tests are committed on this branch; CI verification is pending. The worker still has group-based read/write access to all workspace and snapshot directories. It is therefore not a hostile-tenant isolation boundary.

### Remaining security work

- Adversarial archive and interrupted snapshot recovery tests beyond the current baseline.
- Explicit seccomp/AppArmor/SELinux profile validation and user-namespace assessment.
- Proxy bypass, DNS rebinding, IPv4/IPv6, and metadata endpoint tests.
- Cross-workspace filesystem/process/device/mount adversarial tests.
- Distributed locking before multiple worker processes or replicas.
- Backup/restore and encryption-key recovery drills.

Do not mark the rootless-worker subphase complete until both quality and Linux XFS/Docker integration CI jobs pass on this branch.

