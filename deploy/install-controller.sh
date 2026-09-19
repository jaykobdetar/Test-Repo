#!/bin/bash
# Run manually as administrator from the final reviewed output bundle.
# Starts only the reviewed control/stop/backup services. Never starts paid compute
# or changes global authorization rules/security profiles.
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
umask 077
if [ "$(id -u)" -ne 0 ]; then
  echo "Run this reviewed installer from your own administrator terminal." >&2
  exit 1
fi
if [ "$#" -ne 5 ]; then
  echo "Usage: install-controller.sh BUNDLE_DIR MANIFEST_SHA256 HUMAN_USERNAME RCLONE_CONFIG PRIVATE_PROVIDER_DIR" >&2
  exit 2
fi
PROBE_BUNDLE=$(realpath -- "$1")
PROBE_MANIFEST_SHA=$2
PROBE_HUMAN=$3
PROBE_RCLONE_SOURCE=$(realpath -- "$4")
PROBE_PROVIDER_SOURCE=$(realpath -- "$5")
PROBE_HUMAN_UID=$(id -u "$PROBE_HUMAN")
if [ "$PROBE_HUMAN_UID" -eq 0 ] || [ ! -f "$PROBE_RCLONE_SOURCE" ] || [ ! -f "$PROBE_PROVIDER_SOURCE/runpod.json" ] || [ ! -f "$PROBE_PROVIDER_SOURCE/runpod-api-key" ]; then
  echo "A non-root human identity and prepared private Drive/provider configuration are required." >&2
  exit 1
fi
if [ -L "$PROBE_PROVIDER_SOURCE/runpod.json" ] || [ -L "$PROBE_PROVIDER_SOURCE/runpod-api-key" ]; then
  echo "Private provider handoff files must not be symlinks." >&2
  exit 1
fi
if [ -e /opt/probe-core ] || [ -e /etc/probe-core ]; then
  echo "Existing Probe installation detected; this initial installer never overwrites it." >&2
  exit 1
fi
PROBE_STAGE=$(mktemp -d /opt/.probe-install-XXXXXX)
trap 'rm -rf -- "$PROBE_STAGE"' EXIT
cp -R --no-preserve=all --no-dereference -- "$PROBE_BUNDLE/." "$PROBE_STAGE/"
# Verify the root-owned staged copy, not a mutable path in the caller's account.
/usr/bin/python3 -I - "$PROBE_STAGE" "$PROBE_MANIFEST_SHA" <<'PY'
import hashlib, json, pathlib, re, sys
root = pathlib.Path(sys.argv[1])
if root.stat().st_uid != 0 or root.stat().st_mode & 0o077:
    raise SystemExit('Copied staging directory must remain root-owned and private')
expected = sys.argv[2]
if not re.fullmatch(r'[0-9a-f]{64}', expected):
    raise SystemExit('A pinned release-manifest SHA256 is required')
data = (root / 'release-manifest.json').read_bytes()
if hashlib.sha256(data).hexdigest() != expected:
    raise SystemExit('Release manifest checksum mismatch')
manifest = json.loads(data)
known = {'release-manifest.json'}
for entry in manifest['files']:
    relative = pathlib.PurePosixPath(entry['path'])
    if relative.is_absolute() or any(p in ('', '.', '..') for p in entry['path'].split('/')):
        raise SystemExit('Unsafe release path')
    path = root.joinpath(*relative.parts)
    if not path.resolve().is_relative_to(root):
        raise SystemExit('Release symlink escapes its immutable directory')
    if entry['path'] in known or not path.is_file():
        raise SystemExit('Duplicate or nonregular release member')
    with path.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != entry['sha256']:
            raise SystemExit('Release file checksum mismatch')
    known.add(entry['path'])
actual = {str(p.relative_to(root)) for p in root.rglob('*') if p.is_file() or p.is_symlink()}
if actual != known:
    raise SystemExit('Release contains missing or unreviewed files')
for required in ('python/bin/python3.13', 'controller-requirements.lock', 'deployment.json', 'deploy/install-controller.sh', 'deploy/sandbox/containers.conf'):
    if required not in known:
        raise SystemExit('Release is missing required installation input')
PY
chown -hR root:root "$PROBE_STAGE"
chmod -R go-w "$PROBE_STAGE"
# The metadata-discarding copy removes executable bits even from the runtime.
# Restore only this byte-verified interpreter; keep the staging root private.
chmod 0755 "$PROBE_STAGE/python/bin/python3.13"

# Maintained Ubuntu packages supply the existing distro AppArmor integration.
# This installs packages; it does not disable AppArmor or add polkit grants.
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y podman crun uidmap dbus-user-session rclone acl

for PROBE_GROUP in probe-trusted probe-research probe-watchdog probe-backup probe-ipc probe-ledger-read probe-watch-read probe-stop probe-backup-read; do
  if ! getent group "$PROBE_GROUP" >/dev/null; then groupadd --system "$PROBE_GROUP"; fi
done
for PROBE_ACCOUNT in probe-trusted probe-research probe-watchdog probe-backup; do
  if ! id "$PROBE_ACCOUNT" >/dev/null 2>&1; then
    useradd --system --no-create-home --home-dir "/var/lib/$PROBE_ACCOUNT" --shell /usr/sbin/nologin --gid "$PROBE_ACCOUNT" "$PROBE_ACCOUNT"
  fi
  if [ "$(id -u "$PROBE_ACCOUNT")" -eq 0 ]; then
    echo "A Probe service account unexpectedly has UID0." >&2
    exit 1
  fi
done
usermod -a -G probe-ipc,probe-ledger-read,probe-watch-read,probe-stop,probe-backup-read probe-trusted
usermod -a -G probe-ledger-read,probe-watch-read,probe-stop probe-watchdog
usermod -a -G probe-backup-read probe-backup
usermod -a -G probe-ipc "$PROBE_HUMAN"

# Allocate a nonoverlapping standard subordinate range only when missing.
/usr/bin/python3 -I - <<'PY'
import pathlib, subprocess
for kind, flag in (('subuid', '--add-subuids'), ('subgid', '--add-subgids')):
    path = pathlib.Path('/etc') / kind
    rows = [line.split(':') for line in path.read_text().splitlines() if line.strip()]
    ours = [row for row in rows if row[0] == 'probe-trusted']
    if ours:
        if not any(int(row[2]) >= 65536 for row in ours):
            raise SystemExit('Existing subordinate IDs are too small; administrator review required')
        continue
    start = max([100000, *[int(row[1]) + int(row[2]) for row in rows]])
    subprocess.run(['usermod', flag, f'{start}-{start + 65535}', 'probe-trusted'], check=True)
PY

install -d -o probe-trusted -g probe-ledger-read -m 2750 /var/lib/probe-core
install -d -o probe-trusted -g probe-trusted -m 0700 /var/lib/probe-core/input-artifacts /var/lib/probe-core/worker-transfers /var/lib/probe-provider /var/lib/probe-sandbox
install -d -o probe-watchdog -g probe-watch-read -m 0750 /var/lib/probe-watchdog
install -d -o root -g probe-backup-read -m 0750 /var/lib/probe-backups
install -d -o probe-trusted -g probe-backup-read -m 0750 /var/lib/probe-backups/outbox
install -d -o probe-backup -g probe-backup -m 0700 /var/lib/probe-backup
install -d -o probe-backup -g probe-backup-read -m 0750 /var/lib/probe-backups/receipts
install -d -o probe-research -g probe-research -m 0700 /var/lib/probe-research
install -d -o root -g probe-trusted -m 0750 /etc/probe-core

mv -- "$PROBE_STAGE" /opt/probe-core
PROBE_STAGE=$(mktemp -d /opt/.probe-install-complete-XXXXXX)
/opt/probe-core/python/bin/python3.13 -I -m venv /opt/probe-core/venv
/opt/probe-core/venv/bin/python -I -m pip --isolated install --no-index --require-hashes --find-links /opt/probe-core/wheels -r /opt/probe-core/controller-requirements.lock
# The project wheel is separately covered by the pinned release manifest.
/opt/probe-core/venv/bin/python -I -m pip --isolated install --no-index --no-deps /opt/probe-core/probe_core-*.whl
chown -hR root:root /opt/probe-core
chmod 0755 /opt/probe-core
chmod -R a+rX /opt/probe-core/python /opt/probe-core/venv
chmod -R go-w /opt/probe-core
# Select the maintained OCI runtime for this Probe account only. Both image
# import and the unchanged acceptance service use this HOME and existing store.
install -d -o root -g probe-trusted -m 0750 /var/lib/probe-sandbox/.config /var/lib/probe-sandbox/.config/containers
install -o root -g probe-trusted -m 0640 /opt/probe-core/deploy/sandbox/containers.conf /var/lib/probe-sandbox/.config/containers/containers.conf
/opt/probe-core/venv/bin/python -I - /opt/probe-core "$PROBE_HUMAN" "$PROBE_RCLONE_SOURCE" <<'PY'
import grp, json, os, pathlib, pwd, shutil, sys
from probe_core.host_setup import Identities, copy_gdrive_only, render_configuration
root = pathlib.Path(sys.argv[1])
settings = json.loads((root / 'deployment.json').read_text())
candidate = settings.get('sandbox_image')
if candidate is not None:
    import re
    if not isinstance(candidate, str) or re.fullmatch(r'sha256:[0-9a-f]{64}', candidate) is None:
        raise SystemExit('CPU sandbox image must be an exact local image ID')
    if not (root / 'images/cpu-sandbox.tar').is_file():
        raise SystemExit('CPU sandbox image archive is missing from the reviewed bundle')
identities = Identities.discover(sys.argv[2])
stage = root / 'rendered'
render_configuration(stage, identities=identities, templates=root / 'deploy/live',
                     source_commit=settings['source_commit'], drive_folder_id=settings['drive_folder_id'])
if candidate is not None:
    (stage / 'sandbox-acceptance.env').write_text('SANDBOX_IMAGE=' + candidate + '\n')
for path in stage.iterdir():
    if path.suffix in {'.service', '.timer'}:
        target = pathlib.Path('/etc/systemd/system') / path.name
        shutil.copyfile(path, target)
        target.chmod(0o644)
    else:
        target = pathlib.Path('/etc/probe-core') / path.name
        shutil.copyfile(path, target)
        os.chown(target, 0, grp.getgrnam('probe-trusted').gr_gid)
        target.chmod(0o640)
# Backup configuration is public IDs/commit metadata; the backup identity can
# read that exact file through the independently accessible backup state folder.
shutil.copyfile(stage / 'backup.env', '/var/lib/probe-backup/backup.env')
os.chown('/var/lib/probe-backup/backup.env', identities.backup_uid, grp.getgrnam('probe-backup').gr_gid)
os.chmod('/var/lib/probe-backup/backup.env', 0o600)
copy_gdrive_only(sys.argv[3], '/var/lib/probe-backup/rclone.conf', backup_uid=identities.backup_uid,
                 backup_gid=grp.getgrnam('probe-backup').gr_gid)
shutil.copyfile(root / 'deploy/sandbox/seccomp.json', root / 'seccomp.json')
os.chmod(root / 'seccomp.json', 0o644)
PY
install -o root -g probe-trusted -m 0640 "$PROBE_PROVIDER_SOURCE/runpod.json" /etc/probe-core/runpod.json
install -o probe-trusted -g probe-trusted -m 0600 "$PROBE_PROVIDER_SOURCE/runpod-api-key" /etc/probe-core/runpod-api-key
/opt/probe-core/venv/bin/python -I - <<'PY'
from probe_core.runpod_provider import RunPodConfig
config = RunPodConfig.load('/etc/probe-core/runpod.json')
if config.state_path != '/var/lib/probe-provider/runpod.sqlite' or config.api_key_file != '/etc/probe-core/runpod-api-key':
    raise SystemExit('Provider state/key paths do not match the reviewed installation')
PY
# Type=simple startup does not wait for Python initialization. Create both
# local databases now so the first snapshot cannot race the provider broker.
# RunPodProvider construction creates local state only; it performs no API call.
runuser -u probe-trusted -g probe-trusted -- /opt/probe-core/venv/bin/python -I -c 'from probe_core.runpod_provider import RunPodConfig, RunPodProvider; RunPodProvider(RunPodConfig.load("/etc/probe-core/runpod.json"))'
runuser -u probe-trusted -g probe-ledger-read -- /opt/probe-core/venv/bin/python -I -c 'from probe_core.ledger import Ledger; Ledger("/var/lib/probe-core/research.sqlite").close()'
chown probe-trusted:probe-ledger-read /var/lib/probe-core/research.sqlite
chmod 0640 /var/lib/probe-core/research.sqlite
PROBE_TRUSTED_UID=$(id -u probe-trusted)
loginctl enable-linger probe-trusted
systemctl start "user@$PROBE_TRUSTED_UID.service"
systemctl daemon-reload
PROBE_SANDBOX_IMAGE=$(/opt/probe-core/venv/bin/python -I -c 'import json; print(json.load(open("/opt/probe-core/deployment.json")).get("sandbox_image") or "")')
if [ -n "$PROBE_SANDBOX_IMAGE" ]; then
  # The importing UID and real user bus must match the research service. Input
  # redirection lets only the administrator read the immutable release archive.
  # UID-mapping helpers also require the account's own primary group.
  runuser -u probe-trusted -g probe-trusted -- env HOME=/var/lib/probe-sandbox XDG_RUNTIME_DIR="/run/user/$PROBE_TRUSTED_UID" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$PROBE_TRUSTED_UID/bus" /usr/bin/podman --remote=false load < /opt/probe-core/images/cpu-sandbox.tar
  PROBE_LOADED_IMAGE=$(runuser -u probe-trusted -g probe-trusted -- env HOME=/var/lib/probe-sandbox XDG_RUNTIME_DIR="/run/user/$PROBE_TRUSTED_UID" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$PROBE_TRUSTED_UID/bus" /usr/bin/podman --remote=false image inspect --format '{{.Id}}' "$PROBE_SANDBOX_IMAGE")
  if [ "${PROBE_LOADED_IMAGE#sha256:}" != "${PROBE_SANDBOX_IMAGE#sha256:}" ]; then
    echo "Imported CPU image identity does not match the reviewed release." >&2
    exit 1
  fi
  systemctl start probe-sandbox-acceptance.service
  # A failed acceptance exits before this mutation, leaving the facade closed.
  /opt/probe-core/venv/bin/python -I - "$PROBE_SANDBOX_IMAGE" <<'PY'
import json, os, pathlib, sys
from probe_core.research_service import ServiceConfig
path = pathlib.Path('/etc/probe-core/research.json')
data = json.loads(path.read_text())
data['sandbox_image'] = sys.argv[1]
config = ServiceConfig.model_validate(data)
with path.open('w') as stream:
    stream.write(config.model_dump_json())
    stream.flush()
    os.fsync(stream.fileno())
PY
fi
systemctl enable --now probe-provider-stop.service probe-watchdog.service probe-controller.service probe-research.service
systemctl enable --now probe-backup.timer
systemctl start probe-backup.service
systemctl is-active --quiet probe-provider-stop.service probe-watchdog.service probe-controller.service probe-research.service
echo "Installation complete: control/stop services active; initial verified backup completed; daily backup enabled."
echo "No paid compute was started. Dataset submissions remain disabled; any configured CPU sandbox passed its real acceptance gate."
echo "The exact-human ACL permits admin socket access immediately; no logout is required."
