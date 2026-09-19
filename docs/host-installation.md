# Ubuntu controller installation and identity boundary

`deploy/install-controller.sh` is a manual administrator procedure. Preparing the
bundle or running `probe_core.host_setup` does not create accounts, alter `/etc`,
install packages, or start services. Run the final reviewed installer from the
published output bundle in the user's own terminal. This task's execution
environment cannot acquire administrator privileges through `sudo` because it
already enforces `no_new_privs`; do not disable that restriction.

## Reviewed release inputs

The release bundle must contain a self-contained pinned `python/` runtime,
offline `wheels/`, hash-pinned `controller-requirements.lock`, the project wheel,
`deployment.json`, and reviewed deployment templates. `deployment.json` contains
the reviewed `source_commit` and selected `drive_folder_id`. The controller
environment needs the core, MCP and controller extras, including NumPy and
safetensors for retained CPU-generated tensor inputs; GPU PyTorch dependencies
remain on the separate worker and in the CPU sandbox image.
When `deployment.json` includes `sandbox_image` as an exact local `sha256:` image
ID, the release must also include `images/cpu-sandbox.tar`. This archive and its
image identity are covered by the reviewed release manifest.

The administrator command receives the independently recorded SHA-256 of
`release-manifest.json`. Its `files` array inventories every other bundle file
as `{ "path": "relative/path", "sha256": "64 lowercase hex" }`. Internal
runtime symlinks may point only inside the bundle. The installer first copies
the bundle into private root-owned staging under `/opt`, verifies the manifest
and every copied file, refuses unreviewed extras, then executes the verified
runtime. The copy does not preserve caller ownership, modes or ACLs. Privileged
Python and installed services use isolated imports; installation does not import
code from the caller's working directory. It never copies a venv that points
back to a user-writable Python.

```sh
sudo ./install-controller.sh /absolute/published/bundle MANIFEST_SHA256 HUMAN_USERNAME /absolute/private/rclone.conf /absolute/private/provider
```

The initial installer refuses an existing `/opt/probe-core` or `/etc/probe-core`.
It installs maintained Ubuntu Podman, UID-mapping helpers, user-session D-Bus,
rclone and filesystem ACL packages. It changes no global AppArmor, polkit, or sudoers policy.
The metadata-discarding copy deliberately removes executable permissions. After
verifying the staged bytes, the installer explicitly restores execution only on
the pinned Python interpreter before creating the installed environment.
It creates the following accounts and relevant private directories:

| Identity | Authority |
| --- | --- |
| Human account | Administrative socket; reviews and consumes one-time compute authorization |
| `probe-trusted` | Controller, research facade, dispatcher, provider stop broker and snapshot producer; owns the authoritative ledger |
| `probe-research` | Model-facing MCP/research process; no live ledger, provider credential, admin socket or backup credential access |
| `probe-watchdog` | Read-only ledger plus stop/status broker socket; independent durable state; no provider API key |
| `probe-backup` | Read-only completed snapshot outbox plus its own Google Drive credential and receipts; no live ledger or RunPod credential |

The watchdog's narrow socket is a local privilege boundary. A Pod-capable RunPod
API key is not represented as provider-enforced stop-only access. The independent
broker owns that key and exposes only stop/status operations to the watchdog UID.
Neither process sharing this workstation establishes shutdown after complete
host loss. Unattended execution remains disabled until a separately verified
host-loss cutoff exists; supervised acceptance and ordinary process-failure
tests are separate claims.

The private provider handoff directory contains `runpod.json` and
`runpod-api-key`. It is never part of the release or backup archive. Configuration
must use `state_path=/var/lib/probe-provider/runpod.sqlite` and
`api_key_file=/etc/probe-core/runpod-api-key`. The installer writes the configuration
root-owned and the credential service-owned `0600`; neither secret is printed.
The controller unit sets an ACL/default ACL for the exact human UID on its own
runtime directory at each start. This grants immediate socket access without a
logout or a global authorization rule; kernel peer-UID method checks still apply.

## Rootless sandbox runtime

The installer discovers actual UIDs, allocates missing nonoverlapping subordinate
UID/GID ranges, enables the trusted account's lingering user manager and starts
`user@<trusted-uid>.service`. The generated environment uses
`XDG_RUNTIME_DIR=/run/user/<trusted-uid>` and its actual `bus` socket. Podman state
may use separate storage paths. Do not substitute a workspace or `/tmp` path for
the user runtime: Podman cleanup may retain only XDG_RUNTIME_DIR, and crun can
otherwise fall back to the system bus and trigger authentication prompts.

The research unit retains normal UID-mapping helper behavior. The experiment
container enforces no-new-privileges, dropped capabilities, seccomp, no network,
read-only inputs, and hard resource limits. `ProtectHome=read-only` keeps the user
runtime visible; the exact `/run/user/<trusted-uid>` writable exception permits
Podman state without broadly writable host paths.

If the release includes a CPU image, the installer loads it into the trusted
account's rootless store, verifies its exact image ID, and runs
`probe-sandbox-acceptance.service`. That one-shot unit is derived from the actual
research service profile, including its identity, user bus, cgroup delegation and
filesystem restrictions. The production acceptance command checks actual
containment and resource limits and writes
`/var/lib/probe-sandbox/acceptance-report.json`. Only a successful gate enables
that image in the root-owned facade configuration. A failure stops installation
with CPU execution still disabled; no weaker fallback is selected.

## Final activation gates

The installer starts the reviewed controller, provider stop broker, watchdog and
research facade without submitting any compute request. It performs an initial
verified Drive backup and enables the daily backup timer. It generates a default
empty discovery dataset allowlist. The CPU sandbox stays disabled unless its
bundled image passes the installed service gate. The dispatcher
configuration is explicitly pending, with no accepted worker host or credential.
Before executing research:

1. Verify the installed stop-broker/watchdog identities, health and sockets.
2. Review the successful CPU acceptance receipt from installation; if the release
   omitted a CPU image, import and test it before enabling CPU execution.
3. Install reviewed worker host/key configuration and allowed discovery dataset
   hashes. Match the facade and dispatcher input-store path exactly.
   The pending SSH user is `root`, matching the trusted worker bootstrap's public
   key location. Numerical execution still uses its separate non-root worker UID.
4. Verify filesystem/socket denial from `probe-research`, watchdog credential
   denial, unauthorized-wake rejection, independent shutdown and stopped state.
5. Produce, upload, download and restore one real research snapshot using the
   selected private Drive folder. Retain the verification receipt.

The actual research agent must execute with the restricted research identity.
Running the whole agent as the human administrative UID would also grant it
access to the human approval socket. Fresh prompts or a restricted MCP tool list
alone cannot repair that OS-level identity reuse. An administrator can launch
the model-facing process using `runuser -u probe-research -- ...`; no broad
passwordless sudo rule is supplied. The exact-human ACL avoids depending on the
current session's cached supplementary groups.

## Recovery from the first interpreter permission failure

An earlier installer omitted that interpreter permission restoration and could
stop with `python3.13: Permission denied` immediately after moving the verified
bundle into `/opt/probe-core`. Package installation and account creation had
already completed, but no venv or Probe services had been configured.

For this exact state, `deploy/resume-controller-install.py` rechecks the original
manifest digest, all installed file hashes and ownership, and the absence of
later installation state. It refuses unexpected files, existing services or
nonempty research/backup state. It restores the verified interpreter permission
and runs the remaining commands from the checksum-verified installed installer.
It does not reinstall packages, recreate accounts, or clear any data. Run the
reviewed helper with isolated system Python as administrator and the same human
and private credential paths used in the original installation. The original
bundle and manifest remain unchanged. Other installation failures require their
own diagnosis; this helper is not a general overwrite or reset command.
