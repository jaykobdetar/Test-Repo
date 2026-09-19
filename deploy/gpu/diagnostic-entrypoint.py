"""Expose key-authenticated SSH while a separate controller tests provider stop."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from diagnose import diagnose

public_key = os.environ.pop("PUBLIC_KEY", "").strip()
if not re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/]+={0,2}(?: [^\r\n]+)?", public_key):
    raise SystemExit("PUBLIC_KEY must contain exactly one trusted Ed25519 public key")
root = Path("/root/.ssh")
root.mkdir(mode=0o700, exist_ok=True)
root.chmod(0o700)
keyfile = root / "authorized_keys"
keyfile.write_text(public_key + "\n")
keyfile.chmod(0o600)
subprocess.run(["ssh-keygen", "-l", "-f", str(keyfile)], check=True)
subprocess.run(["ssh-keygen", "-A"], check=True)
for hostkey in Path("/etc/ssh").glob("ssh_host_*_key.pub"):
    subprocess.run(["ssh-keygen", "-l", "-f", str(hostkey)], check=True)
configuration = Path("/etc/ssh/sshd_config.probe")
configuration.write_text("""Port 22
HostKey /etc/ssh/ssh_host_ed25519_key
PermitRootLogin prohibit-password
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitEmptyPasswords no
PubkeyAuthentication yes
AuthorizedKeysFile .ssh/authorized_keys
UsePAM yes
AllowTcpForwarding local
PermitOpen 127.0.0.1:8080
X11Forwarding no
AllowAgentForwarding no
PrintMotd no
Subsystem sftp internal-sftp
""")
report = diagnose(Path(os.environ.get("PROBE_CGROUP_ROOT", "/sys/fs/cgroup")))
directory = Path("/workspace/probe/diagnostics")
directory.mkdir(parents=True, exist_ok=True)
output = directory / ("preflight-" + report["observed_at"].replace(":", "-") + ".json")
output.write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps({"diagnostic_report": str(output), "report": report}), flush=True)
# No model download, inference, API credential, provider request or autonomous
# restart exists here. The independently supervised provider stop ends billing.
os.execv("/usr/sbin/sshd", ["/usr/sbin/sshd", "-D", "-e", "-f", str(configuration)])
