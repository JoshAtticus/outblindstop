"""Unpack the M3200 partition dumps for inspection.

- system / recoveryfs / recovery: UBI images -> ubi_reader extracts the UBIFS
  contents (the whole rootfs) into extracted/<name>/
- boot: checked for an Android bootimg header; kernel+ramdisk split out

Usage:
  python3 -m pip install ubi_reader
  python3 extract.py
"""

import shutil
import site
import struct
import subprocess
import sys
from pathlib import Path

BACKUP = Path("backup")
OUT = Path("extracted")


def find_tool(name):
    p = shutil.which(name)
    if p:
        return p
    # Windows Store python installs user scripts outside PATH
    cand = Path(site.getusersitepackages()).parent / "Scripts" / f"{name}.exe"
    if cand.exists():
        return str(cand)
    sys.exit(f"tool '{name}' not found - run: python3 -m pip install ubi_reader")

UBI_TARGETS = {
    "mtd29_system.bin": "system",
    "mtd25_recoveryfs.bin": "recoveryfs",
    "mtd23_recovery.bin": "recovery",
}

def parse_bootimg(path: Path):
    data = path.read_bytes()
    if data[:8] != b"ANDROID!":
        print(f"{path.name}: not an Android bootimg (magic={data[:8]!r}) - dump it in a hex editor instead")
        return

    (magic, kernel_size, kernel_addr, ramdisk_size, ramdisk_addr,
     second_size, second_addr, tags_addr, page_size, dt_size,
     _unused, name, cmdline, _id) = struct.unpack_from(
        "<8sIIIIIIIIII16s512s8s", data, 0
    )

    hdr_name = name.rstrip(b"\x00").decode(errors="replace")
    hdr_cmdline = cmdline.rstrip(b"\x00").decode(errors="replace")
    print(f"{path.name}: kernel={kernel_size} B, ramdisk={ramdisk_size} B, "
          f"second={second_size} B, dt={dt_size} B, page={page_size}")
    print(f"  name: {hdr_name!r}")
    print(f"  cmdline: {hdr_cmdline!r}")

    n = (kernel_size + page_size - 1) // page_size
    p = page_size
    kernel = data[p:p + kernel_size]
    p += n * page_size
    n = (ramdisk_size + page_size - 1) // page_size
    ramdisk = data[p:p + ramdisk_size]

    outdir = OUT / "boot"
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "zImage").write_bytes(kernel)
    (outdir / "ramdisk.img").write_bytes(ramdisk)
    print(f"  wrote {outdir/'zImage'} and {outdir/'ramdisk.img'}")
    if ramdisk[:2] == b"\x1f\x8b":
        print("  ramdisk is gzip - unpack with: "
              f"python3 -c \"import gzip,shutil;shutil.copyfileobj(gzip.open(r'{(outdir/'ramdisk.img')}'),open(r'{(outdir/'ramdisk.cpio')}','wb'))\"")


def main():
    OUT.mkdir(exist_ok=True)

    for fname, name in UBI_TARGETS.items():
        src = BACKUP / fname
        if not src.exists():
            print(f"missing {src} - skipping {name}")
            continue
        if src.read_bytes()[:8] == b"ANDROID!":
            # some partitions are plain bootimg, not UBI (e.g. recovery)
            parse_bootimg(src)
            continue
        dest = OUT / name
        if dest.exists():
            print(f"extracted/{name} already exists - skipping")
            continue
        print(f"extracting {name} ...")
        r = subprocess.run(
            [find_tool("ubireader_extract_files"), str(src), "-o", str(dest)],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            tail = (r.stdout + r.stderr).strip().splitlines()[-5:]
            print("  FAILED:")
            for l in tail:
                print(f"    {l}")
            print("  (for the system rootfs use fetch_rootfs.py instead -")
            print("   it tar-copies the live mounted filesystem over ssh)")
        else:
            print(f"  -> extracted/{name}/")

    boot = BACKUP / "mtd17_boot.bin"
    if boot.exists():
        parse_bootimg(boot)

    print("\nexplore the rootfs here: extracted/system/  (bin/, etc/, sbin/, usr/)")


if __name__ == "__main__":
    sys.exit(main())
