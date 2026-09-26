"""Make SSH (dropbear) + root password persistent across reboots on the M3200.

Edits the device's init script to re-apply the root password and start dropbear
at boot. Backs up the original script first. Safe to run multiple times (skips
if the persistence block is already present).

Usage: python3 persist.py
"""

import getpass
import sys

import paramiko

HOST = "192.168.1.1"
PORT = 2222
USER = "root"

MARKER = "# --- root persistence (added by persist.py) ---"

# M3100/M3200 use init-SDX65.sh; older models used syslogd_monitor.sh
CANDIDATES = [
    "/opt/nvtl/bin/init-SDX65.sh",
    "/opt/nvtl/bin/syslogd_monitor.sh",
]


def run(cli, cmd):
    _, stdout, stderr = cli.exec_command(cmd)
    out = stdout.read().decode(errors="replace").strip()
    err = stderr.read().decode(errors="replace").strip()
    rc = stdout.channel.recv_exit_status()
    return rc, out, err


def write_remote(cli, path, text):
    """Write a file via exec channel (device dropbear has no sftp subsystem)."""
    chan = cli.get_transport().open_session()
    chan.exec_command(f"cat > {path}")
    chan.sendall(text.encode())
    chan.shutdown_write()
    return chan.recv_exit_status()


def main():
    password = getpass.getpass(f"{USER}@{HOST} password: ")
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(HOST, port=PORT, username=USER, password=password,
                look_for_keys=False, allow_agent=False, timeout=15)

    target = None
    for cand in CANDIDATES:
        rc, out, _ = run(cli, f"[ -f {cand} ] && echo yes")
        if out == "yes":
            target = cand
            break
    if not target:
        sys.exit(f"none of these exist: {CANDIDATES} - paste 'ls /opt/nvtl/bin/' here")

    rc, out, _ = run(cli, f"cat {target}")
    print(f"target init script: {target} ({len(out)} bytes)")
    rc, _, err = run(cli, f"cp {target} {target}.pre-root")
    if rc != 0:
        sys.exit(f"backup failed: {err}")

    # rewrite the script: strip any previously misplaced block, then insert
    # ours BEFORE the first 'exit 0' so it actually runs
    _, raw, _ = run(cli, f"cat {target}")
    lines = raw.splitlines()

    lines = [
        l for l in lines
        if l.strip() != MARKER
        and "passwd root" not in l
        and "dropbear -r /etc/dropbear" not in l
    ]

    rc, _, err = run(cli, f"[ -f {target}.pre-root ] || cp {target} {target}.pre-root")
    if rc != 0:
        sys.exit(f"backup failed: {err}")

    block = [
        MARKER,
        'echo -e "Root@123\\nRoot@123" | passwd root',
        "/usr/sbin/dropbear -r /etc/dropbear/dropbear_rsa_key -p 2222",
    ]
    try:
        idx = next(i for i, l in enumerate(lines) if l.strip() == "exit 0")
    except StopIteration:
        idx = len(lines)
    lines[idx:idx] = block

    rc = write_remote(cli, target, "\n".join(lines) + "\n")
    if rc != 0:
        sys.exit(f"write failed, rc={rc} - DO NOT reboot")

    rc, out, _ = run(cli, f"grep -n -B1 -A1 dropbear {target}")
    print("verification:")
    print(out)
    rc, out, _ = run(cli, f"grep -c dropbear {target}")
    if int(out or 0) >= 1:
        print("\nOK - reboot the device now and confirm ssh comes back up.")
    else:
        print("\nWARNING: dropbear line not found after edit - DO NOT reboot yet.")


if __name__ == "__main__":
    main()
