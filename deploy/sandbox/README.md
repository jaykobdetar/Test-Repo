# Rootless CPU experiment sandbox

This is an execution layer for arbitrary researcher Python. Enable the
researcher-facing executor only after the mandatory real containment gate below
passes on its actual controller host with its exact pinned image and policy.

## Runtime requirements

Use a maintained, patched rootless Podman and OCI runtime on Linux with:

- working subordinate UID/GID mappings and `newuidmap` / `newgidmap`;
- cgroup v2 with delegated CPU, memory and PID controllers;
- seccomp support;
- a dedicated non-root controller OS identity and private local workspace;
- a locally built, vetted image pinned by its immutable image ID.

The implementation verifies Podman's rootless/cgroup/seccomp capabilities and
checks actual container limits before allowing the experiment to start. Missing
capabilities fail closed. It never falls back to Docker, a host Python process,
privileged execution, host networking, disabled seccomp or unlimited cgroups.

Podman must be available to the controller host. Its executable may be specified
explicitly for a locally installed runtime. No Podman daemon or management socket
is needed.
The trusted launcher must retain the normal ability to use Podman's UID-mapping
helpers. Parent-level `NoNewPrivileges` blocks those helpers; some user-service
syscall restrictions, including `RestrictAddressFamilies`, imply that setting.
Keep the experiment restrictions on the actual container and rerun the gate
under the exact deployed launcher identity and service policy.

The `systemd` cgroup manager does require the trusted account's **user session
bus**, including for container removal. During Phase 6 host preparation, enable
the dedicated `probe-trusted` user's manager with `loginctl enable-linger
probe-trusted` and ensure `user@<actual-uid>.service` is running before starting
the research service. These are administrator setup actions on the dedicated
deployment host; the application never configures host accounts or polkit.

Copy `deploy/research-runtime.env.example` to
`/etc/probe-core/research-runtime.env`, replacing `2001` in both values with the
actual UID reported by `id -u probe-trusted`. Replace the same illustrative UID
in `deploy/probe-research.service`'s exact `ReadWritePaths=/run/user/2001` entry.
Keep the file owned by the administrator and readable by the trusted service.
Its `XDG_RUNTIME_DIR` must be `/run/user/<actual-uid>`, with a working `bus` socket,
and `DBUS_SESSION_BUS_ADDRESS` must point to that socket. Do not use `%U` in this
system service template or substitute the sandbox workspace for the user's
runtime directory. `ProtectHome=read-only` keeps that runtime visible; the exact
writable exception permits Podman's per-user state while retaining read-only
home paths. `/run/probe-sandbox` remains a private application runtime directory.
Rerun the mandatory gate under this final identity and service configuration.

## Build and pin the image

Choose a maintained official Python 3.13 slim image and resolve its digest during
the administrator-controlled preparation step. Pass the complete
`docker.io/library/python@sha256:...` reference as `BASE_IMAGE`; never use a
floating tag for a retained environment.

```sh
mkdir -p work/cpu-sandbox-build/wheels
cp deploy/sandbox/Containerfile deploy/sandbox/sandbox_entry.py \
  deploy/sandbox/requirements-cpu.lock work/cpu-sandbox-build/
python -m pip download --only-binary=:all: --require-hashes \
  --index-url https://pypi.org/simple \
  --extra-index-url https://download.pytorch.org/whl/cpu \
  --dest work/cpu-sandbox-build/wheels -r deploy/sandbox/requirements-cpu.lock
podman build --pull=never --network=none \
  --build-arg BASE_IMAGE="$PROBE_PINNED_PYTHON_BASE" \
  --iidfile work/probe-cpu-image.id work/cpu-sandbox-build
```

The Containerfile installs only previously downloaded, hash-verified wheels with
network disabled. The CPU lock targets CPython 3.13 on Linux x86_64 and includes
PyTorch 2.14.0+cpu, Transformers 5.17.0, NNsight 0.7.0, NumPy 2.5.3, SciPy 1.18.1,
scikit-learn 1.9.1, safetensors 0.8.0 and all transitive dependencies. The final
image contains no wheel cache or CUDA libraries. Any dependency change requires
a new image ID and containment run. Default limits are one CPU core, 1 GiB RAM,
32 processes, 30 seconds, 8 MiB artifacts and 1 MiB logs. BLAS/OpenMP use one
thread and Hugging Face is offline.
Runtime execution uses `--pull=never` and accepts only `sha256:<64 hex>` local
image IDs. The entrypoint is fixed by the runner, independent of image defaults.

## Mandatory real containment gate

```sh
PROBE_SANDBOX_REQUIRED=1 \
PROBE_SANDBOX_IMAGE="$(cat work/probe-cpu-image.id)" \
python -m pytest tests/test_sandbox.py tests/test_sandbox_service.py -q
```

Set `PROBE_SANDBOX_PODMAN` to an absolute executable path if necessary. With
`PROBE_SANDBOX_REQUIRED=1`, missing runtime/image/capabilities fail the suite.
Without that flag, the eight integration cases explicitly skip if the environment
is not configured. Passing the unit tests alone does not satisfy this gate.

The real tests exercise forbidden network/socket access, absence of host
credentials and management sockets/GPU devices, read-only root/input mounts,
memory/PID controls, bounded output storage, wall-time termination, cancellation,
the exact-job broker, and real offline tensor/statistical analysis with the
pinned scientific libraries. CPU quotas and no-new-privileges/capability/seccomp state
are checked from inside every container before the workload starts.

## Authority and data flow

`PodmanSandbox.run(code, inputs=..., limits=..., broker=...)` stages only explicit
input bytes and Python source in a private directory, mounts it read-only, and
provides one writable `/output` tmpfs. The tmpfs has a hard byte bound and
its data and inode overhead are charged to the container memory cgroup. There is no writable host volume.
The root filesystem and automatic temporary filesystems are read-only. Code can
write CPU artifacts under `/output`; completed regular files are exported in
bounded frames with hashes into a private per-run directory. Symlinks, hardlinks,
special files, traversal, duplicate paths and incomplete outputs are rejected.
Only artifacts from successful, fully attested executions are returned.

The optional `GPURequestBroker` takes controller-owned approved job keys mapped
to exact validated `JobSpec` objects. Its callback must enforce the normal job
authorization/budget rules and return only a job ID. It does not hold a provider
key, approve a wake, start compute or execute a supplied GPU function.

A sandboxed script requests an existing approved job using one line on stdout:

```text
PROBE_BROKER:{"kind":"gpu_request","request_id":"r1","job_key":"approved-key"}
```

It flushes stdout, then reads one JSON response line from stdin. Extra fields,
unknown job keys and unsupported operations are denied. Identical request IDs
are cached; reuse for a different job is denied. The broker never returns raw
controller exception text. Treat all ordinary stdout/stderr and artifact bytes
as untrusted research data, never control instructions.

No cloud credentials, API credentials, Docker/Podman sockets, arbitrary devices,
host PID namespace or host network are passed to the container. The authoritative seccomp profile is packaged at
`probe_core/resources/seccomp.json`; `deploy/sandbox/seccomp.json` is the identical
reviewable deployment copy. The seccomp allowlist additionally denies socket creation, namespace creation, mounting,
ptrace/process-memory access, keyring operations and BPF. Allowed process
creation remains constrained by the PID and memory cgroups.

Podman's independent `--timeout` deadline and `--rm` cleanup remain active if
the facade and its attached Podman client are killed. The controller also owns
the startup-inclusive wall-time/cancellation watchdog. It kills the container,
cleans up its Podman instance and requires confirmed removal. A cleanup failure
raises `SandboxUnavailable`; callers must not treat that as a completed stop.
Input and returned output directories are retained under the private workspace
for provenance; retention/backup policy belongs to the controller.

The installed `probe_core.sandbox_acceptance` gate additionally starts a fixed
synthetic program, confirms its execution, kills both launchers with SIGKILL,
and requires the container processes to stop and its record to disappear within
the bounded observation window. PID descriptors prevent confusing reused PIDs
with surviving processes. Explicit cleanup happens only after the observation
has passed or failed; it cannot supply evidence for a passing result. Run this
gate under the actual installed service profile after a launcher change.

## Verified environment and image

The mandatory real gate passed on 2026-09-19: **eight real rootless-container
checks passed**, including offline scientific analysis. The full run had 35
passing tests in 108.20 seconds; an additional packaged-profile consistency test
was then added and all 28 non-container unit tests passed separately. A further
real facade integration test passed in 26.90 seconds: the research service
created a safetensors artifact, retained it under a stable ID and typed tensor
references, deleted the temporary source, and supplied that retained ID to a
second CPU container which computed and retained the expected result.

`image-lock.json` records the base image digest, built local scientific image ID,
Python version, and hashes of the CPU dependency lock, entrypoint and seccomp
profile. The verified local image is:

```text
sha256:9d0457022d2d7334a13341e9948a8691e6551bc8655876d8e37233ebd6dce34e
```

The workstation initially lacked Podman. Ubuntu Podman 4.9.3, conmon and crun
were downloaded and extracted into the task's private `work/` directory without
installing system packages. Direct execution from Codex could not create the
subordinate UID mappings: its process already has `NoNewPrivs=1` and a nested
single-UID namespace. The native host supports writable delegated cgroup-v2
CPU/memory/PID controllers and valid subordinate UID/GID mappings.

The tests therefore ran in a temporary **unprivileged user systemd service**
with `Delegate=yes`, under Ubuntu's already installed `podman` AppArmor profile
using `aa-exec --profile=podman`. The explicit profile was necessary because the
extracted binary lives outside the distribution profile's normal `/usr/bin/podman`
path. This changed no host security policy, disabled no containment control and
installed no persistent service. It is a test invocation, not a production service
deployment. A dedicated controller host should install its maintained distro
Podman package normally and rerun the mandatory gate under its service identity.

The scientific image was built from pinned input with `--network=none`; only the
administrator preparation step downloaded public packages. Tests launched no
cloud resources and exposed no GPU device. The temporary CPU containers were
force-removed after each run, including timeout and cancellation cases.

An additional cleanup check on 2026-09-19 found and corrected an integration
error in the temporary local wrapper: it had used its private Podman runroot as
`XDG_RUNTIME_DIR`. Podman 4.9.3 passes only `XDG_RUNTIME_DIR` to its direct runtime
delete command; crun 1.14.1 falls back to the system bus when it cannot connect to
the user bus. This caused interactive authorization prompts to stop `libpod`
units. Restoring `/run/user/1000` as the user's runtime, while retaining a
separate private Podman `--runroot`, fixed cleanup. The real containment check
passed again in 8.45 seconds, no polkit entries appeared after its 10:45:11 UTC
start, and `podman ps --all` returned an empty list. No polkit grants or host
security policy changes were introduced. The local wrapper now refuses to launch
without the user's bus socket and initializes its private pause-process directory.
See the pinned [Podman delete implementation](https://github.com/containers/podman/blob/v4.9.3/libpod/oci_conmon_common.go#L468-L474)
and [crun bus selection](https://github.com/containers/crun/blob/1.14.1/src/libcrun/cgroup-systemd.c#L706-L720).

References: [Podman run options](https://docs.podman.io/en/latest/markdown/podman-run.1.html),
[Podman rootless setup](https://github.com/podman-container-tools/podman/blob/main/docs/tutorials/rootless_tutorial.md),
[Ubuntu user-namespace policy](https://ubuntu.com/blog/ubuntu-23-10-restricted-unprivileged-user-namespaces).
