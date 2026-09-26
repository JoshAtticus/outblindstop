"""Pull the live rootfs off the M3200 as a tar stream over SSH.

ubireader can't parse this build's UBIFS master node, but the rootfs is mounted
on the device anyway - tarring it is simpler and reflects exactly what runs.

Usage: python3 fetch_rootfs.py
Output: extracted/rootfs.tar (then unpacked to extracted/rootfs/)
"""

import getpass
import sys
import tarfile
from pathlib import Path

import paramiko

HOST = "192.168.1.1"
PORT = 2222
USER = "root"

# top-level dirs that make up the rootfs (skip pseudo-fs and volatile dirs)
DIRS = "bin etc lib opt root sbin usr var home www persist"


def main():
    out_dir = Path("extracted")
    out_dir.mkdir(exist_ok=True)
    tar_path = out_dir / "rootfs.tar"

    password = getpass.getpass(f"{USER}@{HOST} password: ")
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(HOST, port=PORT, username=USER, password=password,
                look_for_keys=False, allow_agent=False, timeout=15)

    _, stdout, stderr = cli.exec_command(f"cd / && tar -cf - {DIRS}")
    total = 0
    with tar_path.open("wb") as f:
        while True:
            chunk = stdout.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            total += len(chunk)
            print(f"\r{total / 1048576:.1f} MiB", end="", flush=True)
    print()
    err = stderr.read().decode(errors="replace").strip()
    rc = stdout.channel.recv_exit_status()
    if rc != 0 and total == 0:
        sys.exit(f"tar failed: {err}")
    if err:
        print(f"(tar stderr, usually harmless file-changed warnings): {err[-300:]}")
    print(f"saved {tar_path} ({total / 1048576:.1f} MiB)")

    dest = out_dir / "rootfs"
    if dest.exists():
        print(f"{dest} already exists - leaving tar as-is; delete it to re-unpack")
        return
    print("unpacking ...")
    links = []
    skipped = 0
    with tarfile.open(tar_path) as tf:
        for m in tf:
            # busybox applet symlinks are absolute (/bin/busybox.nosuid) which
            # tarfile's data filter rejects - record them instead of creating
            if m.issym() or m.islnk():
                links.append((m.name, m.linkname))
                continue
            try:
                tf.extract(m, dest, filter="data")
            except Exception as e:
                skipped += 1
                print(f"  skip {m.name}: {type(e).__name__}")
    (dest / "symlinks.txt").write_text(
        "\n".join(f"{n} -> {t}" for n, t in links) + "\n", encoding="utf8"
    )
    print(f"done -> {dest}/ ({len(links)} symlinks listed in symlinks.txt, {skipped} skipped)")


if __name__ == "__main__":
    sys.exit(main())
