"""Dump every MTD partition of the device over SSH using paramiko (password auth).

Usage: python3 backup.py
Output goes to ./backup/mtdN_name.bin

Streams directly to the PC because the device's /tmp is tmpfs and far too small for
the ~700MB system partition
"""

import getpass
import os
import re
import sys

import paramiko

OUT_DIR = "backup"
HOST = "192.168.1.1"
PORT = 2222
USER = "root"


def connect(password):
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        cli.connect(HOST, port=PORT, username=USER, password=password,
                    look_for_keys=False, allow_agent=False, timeout=15)
        return cli
    except paramiko.ssh_exception.SSHException:
        # old dropbear: disable rsa-sha2 so only legacy ssh-rsa is offered
        transport = paramiko.Transport((HOST, PORT))
        transport.disabled_algorithms = {"pubkeys": ["rsa-sha2-256", "rsa-sha2-512"]}
        transport.start_client(timeout=15)
        if not transport.is_verified():
            raise SystemExit("host key negotiation failed")
        transport.auth_password(USER, password)
        cli._transport = transport
        return cli


def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    password = getpass.getpass(f"{USER}@{HOST} password: ")
    cli = connect(password)

    _, stdout, _ = cli.exec_command("cat /proc/mtd")
    mtd = stdout.read().decode(errors="replace")

    parts = []
    for line in mtd.splitlines()[1:]:
        # /proc/mtd lines: mtdN: <size> <erasesize> "name" - skip the erasesize column
        m = re.match(r'mtd(\d+):\s+([0-9a-fA-F]+)\s+[0-9a-fA-F]+\s+"(.*)"', line.strip())
        if m:
            parts.append((int(m.group(1)), int(m.group(2), 16), m.group(3)))

    if not parts:
        sys.exit(f"could not parse /proc/mtd:\n{mtd}")

    print(f"found {len(parts)} partitions\n")
    for num, size, name in parts:
        out = os.path.join(OUT_DIR, f"mtd{num}_{name}.bin")
        print(f"dumping mtd{num} '{name}' ({size / 1048576:.1f} MiB) -> {out}")
        _, stdout, _ = cli.exec_command(f"dd if=/dev/mtdblock{num} bs=4096")
        written = 0
        with open(out, "wb") as f:
            while True:
                chunk = stdout.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                written += len(chunk)
        rc = stdout.channel.recv_exit_status()
        actual = os.path.getsize(out)
        if rc != 0 or actual != size:
            print(f"  WARNING: exit={rc}, got {actual} bytes, expected {size}")

    print("\ndone. verify sizes above - every line must match, no warnings.")
    print("Copy the backup/ folder somewhere safe before touching anything else.")


if __name__ == "__main__":
    main()
