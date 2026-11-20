# DevBox — Build and Test System

## 1. Purpose

The build system turns an isolated workspace into reproducible build, test, analysis, and artifact jobs. It must report exactly what ran and what happened. A successful build is not proof that an application behaves correctly at runtime.

## 2. Execution modes

### Mode A — Build
Compile, package, or otherwise build software using a configured toolchain. No target application launch is required.

### Mode B — Automated test
Run unit tests, integration tests, linters, formatters, and static analysis. Tests may use mocks or test services where appropriate.

### Mode C — Runtime validation (later)
Launch the built application in an appropriate runtime environment. Some applications require a specific OS, GUI, device, game installation, or GPU. This mode is optional and separately authorized.

## 3. Build profiles

A build profile describes:
- Base image and operating system.
- Language runtime and compiler versions.
- Dependency installation steps.
- Build and test commands.
- Environment variables (references to secrets, never secret values).
- CPU, memory, storage, process, and timeout limits.
- Network access requirements.
- Artifact paths and retention policy.

Profiles should be versioned and reproducible. Avoid silently upgrading compiler or dependency versions between runs.

Initial profiles can cover common stacks (for example, Python, Node.js, and .NET). Add specialized profiles only when a real project requires them.

## 4. Job contract

Every job should record:
- Unique job ID and workspace ID.
- Requesting user/agent.
- Operation and selected profile.
- Source commit and working-tree state at job start.
- Toolchain/image versions.
- Start/end time and duration.
- State: queued, preparing, running, succeeded, failed, cancelled, or timed out.
- Exit code and resource usage.
- Log references and artifact references.

The API should return a job ID promptly. Clients can poll job status or subscribe to log events. Logs must be bounded and stored durably.

## 5. Reproducibility

- Pin base image versions by digest for production profiles.
- Respect lockfiles and pinned dependency versions.
- Record dependency resolution output.
- Record compiler/runtime versions.
- Capture the source commit and dirty diff context.
- Keep build commands explicit and versioned.
- Allow rerunning a failed job with the same profile and inputs.

A workspace may be mutable between jobs; record the exact starting state for every run.

## 6. Resource and process controls

Each job must have configurable limits for CPU, memory, disk, process count, output size, and wall-clock duration. Provide cancellation and ensure child processes are terminated. Do not allow a job to starve the control plane or other workspaces.

## 7. Logs and artifacts

- Stream stdout/stderr while a job runs.
- Persist complete logs within configured limits.
- Include timestamps and stream identity.
- Redact credentials and sensitive environment values.
- Capture artifacts only from approved workspace paths.
- Calculate content hashes and store artifact metadata.
- Make artifact downloads subject to workspace authorization.
- Preserve failure logs long enough for debugging.

## 8. Example: compiling a GTA V C# mod

Compilation does not inherently require launching GTA V or having a dedicated GPU.

A suitable build environment may need:
- A compatible .NET SDK or compiler/build tools.
- Project-specific NuGet dependencies.
- The correct GTA/mod-framework reference assemblies, such as the versions expected by the project.
- Build configuration and target framework compatible with the mod loader/runtime.

The build job can produce a DLL and report compiler errors without starting the game. Automated tests can validate logic that is separated from game APIs, using mocks or adapters where practical.

However:
- Compilation does not prove that the mod loads.
- Unit tests do not prove in-game behaviour.
- Runtime validation may require Windows, game files, mod loader, graphics, and a suitable licensed installation.
- Such runtime testing belongs in a separate, explicitly provisioned environment, not in the ordinary CPU-first build worker.

## 9. GPU policy

The initial build and test platform must not require a dedicated GPU. CPU, memory, and storage are allocated from actual worker-host capacity; virtual quotas do not create additional physical resources.

GPU execution is a later optional worker class. GPU memory cannot be assigned arbitrarily to an ordinary container. It requires supported device passthrough, sharing, or virtualization and compatible host hardware/drivers.

## 10. Failure handling

Distinguish:
- Dependency resolution failure.
- Toolchain/configuration failure.
- Compilation/build failure.
- Test failure.
- Infrastructure/worker failure.
- Timeout or resource-limit termination.
- Cancellation.

Return actionable summaries with links/references to the underlying logs. Never report infrastructure failure as a source-code error without evidence.
