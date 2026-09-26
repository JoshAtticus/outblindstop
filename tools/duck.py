"""Install Spinning Duck permanently on the device.

1. ffmpeg extracts 36 evenly-spaced 320x240 RGB PNGs (even sampling of the
   whole video = smooth rotation loop, regardless of source fps)
2. frames pushed to /data/duck (persists across reboots, unlike /tmp)
3. installs /opt/nvtl/bin/duck - run `duck` over ssh to play on the screen,
   Ctrl+C to stop

mifi_display_png needs <prefix><N>.png, 1-indexed, RGB color type, no alpha.

Usage: python3 duck.py
"""

import subprocess
import sys
from pathlib import Path

import paramiko

# resolve everything relative to this script so it works from any cwd
HERE = Path(__file__).parent

HOST = "192.168.1.1"
PORT = 2222
USER = "root"
PASSWORD = "Root@123"

FRAMES = 36
FPS = 12
USEC_DELAY = int(1_000_000 / FPS)
FRAME_DIR = HERE / "frames"
VIDEO = HERE / "duck.mp4"
DEVICE_FRAME_DIR = "/data/duck"
DUCK_SCRIPT = """#!/bin/sh
# SpinningDuck on the M3200 screen - frames in /data/duck
export LD_LIBRARY_PATH="${LD_LIBRARY_PATH}:/opt/nvtl/lib"
DIR=/data/duck
COUNT=__COUNT__
DELAY=__DELAY__

CHILD=""
stop() {
    [ -n "$CHILD" ] && kill "$CHILD" 2>/dev/null
    exit 0
}
trap stop INT TERM

while :; do
    /opt/nvtl/bin/mifi_display_png "$DIR/duck_" "$COUNT" "$DELAY" &
    CHILD=$!
    wait "$CHILD"
done
""".replace("__COUNT__", str(FRAMES)).replace("__DELAY__", str(USEC_DELAY))


def extract_frames():
    FRAME_DIR.mkdir(exist_ok=True)
    if (FRAME_DIR / f"duck_1.png").exists():
        print("frames already extracted - skipping ffmpeg")
        return
    vf = (
        "scale=320:240:force_original_aspect_ratio=decrease,"
        f"pad=320:240:(ow-iw)/2:(oh-ih)/2:color=white,"
        f"fps={FRAMES / 105.07:.5f}"
    )
    r = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(VIDEO),
         "-vf", vf, "-pix_fmt", "rgb24",
         str(FRAME_DIR / "duck_%d.png")],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        sys.exit(f"ffmpeg failed: {r.stderr[-400:]}")
    n = len(list(FRAME_DIR.glob("duck_*.png")))
    print(f"extracted {n} frames")
    if n != FRAMES:
        print(f"  note: got {n}, expected {FRAMES}")


def push_file(cli, path, data):
    chan = cli.get_transport().open_session()
    chan.exec_command(f"cat > {path}")
    chan.sendall(data)
    chan.shutdown_write()
    return chan.recv_exit_status()


def main():
    extract_frames()

    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(HOST, port=PORT, username=USER, password=PASSWORD,
                look_for_keys=False, allow_agent=False, timeout=15)

    def run(cmd, t=30):
        _, o, e = cli.exec_command(cmd, timeout=t)
        out = o.read().decode(errors="replace").strip()
        rc = o.channel.recv_exit_status()
        return rc, out or e.read().decode(errors="replace").strip()

    rc, out = run(f"mkdir -p {DEVICE_FRAME_DIR}")

    # install the duck command if missing
    rc, out = run("grep -c SpinningDuck /opt/nvtl/bin/duck 2>/dev/null || echo 0")
    if out.strip() == "0":
        if push_file(cli, "/opt/nvtl/bin/duck", DUCK_SCRIPT.encode()) != 0:
            sys.exit("failed to write /opt/nvtl/bin/duck")
        run("chmod +x /opt/nvtl/bin/duck")
        print("installed /opt/nvtl/bin/duck")
    else:
        print("/opt/nvtl/bin/duck already installed")

    # push any frames the device is missing
    frames = sorted(FRAME_DIR.glob("duck_*.png"), key=lambda p: int(p.stem.split("_")[1]))
    for f in frames:
        remote = f"{DEVICE_FRAME_DIR}/{f.name}"
        rc, out = run(f"[ -f {remote} ] && echo yes || echo no")
        if out.strip() == "no":
            if push_file(cli, remote, f.read_bytes()) != 0:
                sys.exit(f"upload failed: {f.name}")
    rc, out = run(f"ls {DEVICE_FRAME_DIR} | wc -l")
    print(f"device has {out} frames in {DEVICE_FRAME_DIR}")

    # sanity check one frame (surfaces PNG format errors immediately)
    rc, out = run(
        f"LD_LIBRARY_PATH=/opt/nvtl/lib /opt/nvtl/bin/mifi_display_png "
        f"{DEVICE_FRAME_DIR}/duck_1 2>&1"
    )
    print(f"single-frame test: rc={rc} {out}")
    if rc != 0:
        sys.exit("single frame failed")

    print("\ndone. ssh in and run:  duck   (Ctrl+C stops it)")
    print("frames + script survive reboots.")


if __name__ == "__main__":
    main()
