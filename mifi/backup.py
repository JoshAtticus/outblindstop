from pathlib import Path
import re

from .device import probe_root, show_device_screen, ssh_connect, ssh_exec
from .ui import (
    banner,
    clear,
    fail,
    ok,
    pause,
    warn,
    BOLD,
    CYAN,
    GREEN,
    MAGENTA,
    RED,
    RESET,
)


def flow_backup():
    clear()
    banner()
    print(f"  {BOLD}{MAGENTA}Backup (dump all MTD partitions){RESET}\n")
    up, authed = probe_root()
    if not authed:
        fail("root required (run Rooting first)")
        pause()
        return

    cli = ssh_connect()
    out_dir = Path("backup")
    out_dir.mkdir(exist_ok=True)

    rc, mtd, _ = ssh_exec(cli, "cat /proc/mtd")
    parts = []
    for line in mtd.splitlines()[1:]:
        m = re.match(r'mtd(\d+):\s+([0-9a-fA-F]+)\s+[0-9a-fA-F]+\s+"(.*)"', line.strip())
        if m:
            parts.append((int(m.group(1)), int(m.group(2), 16), m.group(3)))
    if not parts:
        fail("could not parse /proc/mtd")
        pause()
        return

    warn("these dumps are your ONLY recovery path (no firehose exists).")
    warn("copy the backup/ folder somewhere safe when this finishes.\n")

    show_device_screen(cli, "operation-inprogress")

    bad = 0
    for num, size, name in parts:
        out = out_dir / f"mtd{num}_{name}.bin"
        print(f"  {CYAN}mtd{num}{RESET} {name:<15} {size / 1048576:7.1f} MiB ... ", end="", flush=True)
        _, stdout, _ = cli.exec_command(f"dd if=/dev/mtdblock{num} bs=4096")
        with open(out, "wb") as f:
            while True:
                chunk = stdout.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
        rc = stdout.channel.recv_exit_status()
        actual = out.stat().st_size
        if rc != 0 or actual != size:
            print(f"{RED}FAILED (rc={rc}, {actual} bytes){RESET}")
            bad += 1
        else:
            print(f"{GREEN}ok{RESET}")

    if bad:
        fail(f"{bad} partitions failed, re-run the backup")
    else:
        ok(f"all {len(parts)} partitions dumped to {out_dir}/, zero warnings")
        show_device_screen(cli, "operation-done")
    cli.close()
    pause()
