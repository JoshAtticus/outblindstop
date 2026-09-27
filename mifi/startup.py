import hashlib
import io
import json
from pathlib import Path
import re
import shlex
import shutil
import struct
import subprocess
import tarfile
import tempfile
import time
import zipfile

from .constants import (
    ANIM_H,
    ANIM_SH,
    ANIM_W,
    BACKUP_IMAGES,
    BUNDLED_SCREENS_DIR,
    BUNDLED_STARTUP_ZIP,
    HOST,
    IMAGE_GROUPS,
    IMAGES_DIR,
    SCREEN_ALIASES,
    STARTUP_DIR,
    STARTUP_LOCAL,
    STOCK_DELAY_US,
)
from .device import (
    push_bytes,
    show_device_screen,
    ssh_ensure,
    ssh_exec,
)
from .files import push_to_device
from .ui import (
    banner,
    clear,
    fail,
    info,
    menu,
    multi_menu,
    ok,
    pause,
    prompt,
    warn,
    BOLD,
    CYAN,
    DIM,
    MAGENTA,
    RESET,
)


def _frame_num(name):
    m = re.search(r"(\d+)", Path(name).stem)
    return int(m.group(1)) if m else 0


def startup_params(cli):
    rc, script, _ = ssh_exec(cli, f"cat {STARTUP_DIR}/mifi_display_animation.sh 2>/dev/null")
    num = re.search(r"NUM_FILES=(\d+)", script)
    delay = re.search(r"USLEEP=(\d+)", script)
    n = int(num.group(1)) if num else None
    d = int(delay.group(1)) if delay else STOCK_DELAY_US
    if n is None:
        _, out, _ = ssh_exec(cli, f"ls {STARTUP_DIR}/animation_*.png 2>/dev/null | wc -l")
        try:
            n = int(out)
        except ValueError:
            n = 0
    return n, d, (script if rc == 0 else None)


def _prepare_frame_bytes(data):
    if len(data) > 26 and data[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", data[16:24])
        if (w, h) == (ANIM_W, ANIM_H) and data[25] == 2:
            return data
    try:
        from PIL import Image, ImageOps
    except ImportError:
        raise RuntimeError("frames need converting to 320x240 RGB but PIL is "
                           "missing (pip install pillow)")
    img = Image.open(io.BytesIO(data)).convert("RGB")
    if img.size != (ANIM_W, ANIM_H):
        img = ImageOps.pad(img, (ANIM_W, ANIM_H), color=(0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _group_label_for(arcname):
    base = (arcname.split("/", 1)[1] if "/" in arcname else arcname).lower()
    if "spinner" in base or "ucf_animation" in base:
        return "Loading spinners"
    if "off" in base or "power" in base or "shutdown" in base:
        return "Power-off screen"
    if "restart" in base or "reboot" in base:
        return "Restart screen"
    if "reset" in base:
        return "Reset screen"
    return base


def _image_targets(data, arcname):
    base = arcname.split("/", 1)[1] if "/" in arcname else arcname
    low = base.lower()
    if low in SCREEN_ALIASES:
        return [(t, data) for t in SCREEN_ALIASES[low]]
    canonical = {t.lower() for group in SCREEN_ALIASES.values() for t in group}
    if low in canonical:
        return [(base, data)]
    for group in SCREEN_ALIASES.values():
        stem = group[0].lower().replace("deviceimages_deviceui_", "").replace(".png", "")
        if stem in low:
            return [(t, data) for t in group]
    return [(base, data)]


def _cat_bytes(cli, remote):
    _, stdout, _ = cli.exec_command(f"cat {shlex.quote(remote)}")
    data = stdout.read()
    if stdout.channel.recv_exit_status() != 0 or not data:
        return None
    return bytes(data)


def startup_backup(cli):
    """Back up EVERYTHING animation-related on the device:
    - All boot animation frames + script from /opt/nvtl/data/branding/startup
    - All deviceui images (power off, restart, reset, spinners, etc.)
    Packs into a single timestamped unified bundle in startup/."""
    zip_path = STARTUP_LOCAL / f"animation-backup-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    show_device_screen(cli, "operation-inprogress")
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        _, stdout, _ = cli.exec_command(f"tar czf - {shlex.quote(STARTUP_DIR)}")
        tarball = tmpdir / "_startup.tar.gz"
        with open(tarball, "wb") as f:
            while True:
                chunk = stdout.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
        if stdout.channel.recv_exit_status() != 0 or tarball.stat().st_size == 0:
            fail(f"tar failed on device - does {STARTUP_DIR} exist?")
            return None

        expected = STARTUP_DIR.strip("/")
        frame_files = []
        script_text = None
        with tarfile.open(tarball, "r:gz") as tf:
            for m in tf.getmembers():
                if not m.isfile():
                    continue
                rel = m.name[2:] if m.name.startswith("./") else m.name
                if not (rel == expected or rel.startswith(expected + "/")):
                    continue
                base = rel[len(expected):].lstrip("/")
                target = tmpdir / base
                with tf.extractfile(m) as src, open(target, "wb") as out:
                    out.write(src.read())
                if base == "mifi_display_animation.sh":
                    script_text = target.read_text(errors="replace")
                elif base.startswith("animation_") and base.endswith(".png"):
                    frame_files.append(target)
        tarball.unlink()

        if script_text is None:
            script_text = ANIM_SH
        m = re.search(r"USLEEP=(\d+)", script_text)
        delay = int(m.group(1)) if m else STOCK_DELAY_US

        rc, out, _ = ssh_exec(cli, f"ls -1 {shlex.quote(IMAGES_DIR)} 2>/dev/null")
        image_names = set(BACKUP_IMAGES)
        if rc == 0 and out.strip():
            for line in out.splitlines():
                line = line.strip()
                if line.endswith((".png", ".gif")):
                    image_names.add(line)

        extra_files = []
        for name in sorted(image_names):
            data = _cat_bytes(cli, f"{IMAGES_DIR}/{name}")
            if data:
                extra_files.append((name, data))

        zip_path.parent.mkdir(parents=True, exist_ok=True)
        meta = {
            "format": "mifi-unified-bundle",
            "num_frames": len(frame_files),
            "delay_us": int(delay),
            "resolution": f"{ANIM_W}x{ANIM_H}",
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "origin": f"device backup from {HOST}",
            "images": [a for a, _ in extra_files],
        }
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("metadata.json", json.dumps(meta, indent=2))
            for f in sorted(frame_files, key=lambda p: _frame_num(p.name)):
                zf.write(f, f"animation_{_frame_num(f.name)}.png")
            zf.writestr("mifi_display_animation.sh", script_text)
            for arcname, data in extra_files:
                zf.writestr(f"images/{arcname}", data)

    show_device_screen(cli, "operation-done")
    ok(f"backup created: {meta['num_frames']} boot frames + {len(extra_files)} UI screens/spinners -> {zip_path}")
    return zip_path


def read_bundle(source_path):
    src = Path(source_path)
    if not src.exists():
        return None

    if src.is_dir():
        image_assets = []
        for f in sorted(src.iterdir()):
            if f.is_file() and f.suffix.lower() in (".png", ".gif"):
                image_assets.append((f.name, f.read_bytes()))
        parts = []
        for arcname, _ in image_assets:
            lbl = _group_label_for(arcname)
            if lbl not in parts:
                parts.append(lbl)
        return {
            "source": src,
            "is_dir": True,
            "meta": {},
            "boot_frames": [],
            "script_text": None,
            "delay_us": STOCK_DELAY_US,
            "image_assets": image_assets,
            "available_parts": parts,
        }

    try:
        zf = zipfile.ZipFile(src)
    except zipfile.BadZipFile:
        return None

    with zf:
        names = zf.namelist()
        try:
            meta = json.loads(zf.read("metadata.json").decode())
        except (KeyError, ValueError):
            meta = {}

        boot_frames = sorted(
            [n for n in names if n.lower().endswith(".png") and not n.startswith("images/")],
            key=_frame_num,
        )
        script_text = None
        if "mifi_display_animation.sh" in names:
            script_text = zf.read("mifi_display_animation.sh").decode(errors="replace")
        delay_us = int(meta.get("delay_us") or STOCK_DELAY_US)
        if script_text:
            m = re.search(r"USLEEP=(\d+)", script_text)
            if m:
                delay_us = int(m.group(1))

        image_names = sorted(
            [n for n in names if n.startswith("images/") and not n.endswith("/")],
            key=lambda n: n.lower(),
        )
        image_assets = [(n.split("/", 1)[1], zf.read(n)) for n in image_names]

    parts = []
    if boot_frames:
        parts.append("Boot animation")
    for arcname, _ in image_assets:
        lbl = _group_label_for(arcname)
        if lbl not in parts:
            parts.append(lbl)

    return {
        "source": src,
        "is_dir": False,
        "meta": meta,
        "boot_frames": boot_frames,
        "script_text": script_text,
        "delay_us": delay_us,
        "image_assets": image_assets,
        "available_parts": parts,
    }


def bundle_install(cli, bundle_info, chosen_parts=None, confirm=True):
    if isinstance(bundle_info, (str, Path)):
        bundle_info = read_bundle(bundle_info)
    if not bundle_info:
        fail("invalid bundle")
        return False

    avail = bundle_info["available_parts"]
    if not avail:
        fail(f"no animation or screen assets found in {bundle_info['source']}")
        return False

    src = bundle_info["source"]
    boot_frames = bundle_info["boot_frames"]
    delay_us = bundle_info["delay_us"]
    image_assets = bundle_info["image_assets"]
    num = len(boot_frames)

    if chosen_parts is None:
        if confirm:
            clear()
            banner()
            print(f"  {BOLD}{MAGENTA}Install bundle: {src.name}{RESET}\n")
            if boot_frames:
                print(f"  boot animation: {num} frames ({delay_us / 1000:.1f} ms/frame, {num * delay_us / 1e6:.2f} s)")
            if image_assets:
                print(f"  screens/spinners: {len(image_assets)} file(s) ({', '.join(avail)})")
            print()
            warn(f"this will overwrite selected assets in {STARTUP_DIR} and {IMAGES_DIR}.")
            if not list(STARTUP_LOCAL.glob("animation-backup-*.zip")):
                warn("no device backup exists yet - make one first so you can revert")
                if menu("Backup device assets now?", ["Yes", "No (risky)"]) == 0:
                    if not startup_backup(cli):
                        return False

            if len(avail) == 1:
                if menu(f"Install {avail[0]}?", ["Cancel", "Install"]) != 1:
                    return False
                chosen_parts = avail
            else:
                picked = multi_menu(f"Select components to install from {src.name}", [
                    *avail,
                    "Everything",
                ])
                if not picked:
                    return False
                if "Everything" in picked:
                    chosen_parts = avail
                else:
                    chosen_parts = picked
        else:
            chosen_parts = avail

    want_boot = "Boot animation" in chosen_parts and bool(boot_frames)
    want_screens = [p for p in chosen_parts if p != "Boot animation"]

    cli = ssh_ensure(cli)
    if not cli:
        return False

    installed_boot = False
    if want_boot:
        show_device_screen(cli, "operation-inprogress")
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp) / "startup"
            stage.mkdir()
            try:
                with zipfile.ZipFile(src) as zf:
                    for i, name in enumerate(boot_frames, 1):
                        (stage / f"animation_{i}.png").write_bytes(
                            _prepare_frame_bytes(zf.read(name)))
            except Exception as e:
                fail(f"error unpacking frames: {e}")
                return False

            script_text = bundle_info["script_text"] or ANIM_SH
            script_text = re.sub(r"NUM_FILES=\d+", f"NUM_FILES={num}", script_text)
            script_text = re.sub(r"USLEEP=\d+", f"USLEEP={delay_us}", script_text)
            if "__NUM_FILES__" in script_text:
                script_text = (ANIM_SH.replace("__NUM_FILES__", str(num))
                                      .replace("__USLEEP__", str(delay_us)))
            script_text = script_text.replace("\r\n", "\n").replace("\r", "\n")
            (stage / "mifi_display_animation.sh").write_bytes(script_text.encode())

            ssh_exec(cli, f"rm -f {STARTUP_DIR}/animation_*.png")
            if not push_to_device(cli, stage, STARTUP_DIR):
                return False

            local_md5 = hashlib.md5((stage / "animation_1.png").read_bytes()).hexdigest()
            _, out, _ = ssh_exec(cli, f"md5sum {STARTUP_DIR}/animation_1.png")
            if not out or out.split()[0] != local_md5:
                fail("md5 mismatch after push for frame 1!")
                return False

        ssh_exec(cli, f"chmod +x {STARTUP_DIR}/mifi_display_animation.sh")
        ok(f"boot animation installed: {num} frames, {delay_us / 1000:.1f} ms/frame")
        show_device_screen(cli, "operation-done")
        installed_boot = True

    installed_screens_count = 0
    if want_screens and image_assets:
        target_assets = [
            (arcname, data) for arcname, data in image_assets
            if _group_label_for(arcname) in want_screens
        ]
        if target_assets:
            targets = []
            for arcname, data in target_assets:
                targets.extend(_image_targets(data, arcname))

            staged = []
            try:
                for name, data in targets:
                    if name.lower().endswith(".gif"):
                        staged.append((name, data))
                    else:
                        staged.append((name, _prepare_frame_bytes(data)))
            except RuntimeError as e:
                fail(str(e))
                return False

            cli = ssh_ensure(cli)
            if not cli:
                return False
            show_device_screen(cli, "operation-inprogress")
            for name, data in staged:
                if push_bytes(cli, f"{IMAGES_DIR}/{name}", data) != 0:
                    fail(f"push failed for {name}")
                    return False
                local_md5 = hashlib.md5(data).hexdigest()
                _, out, _ = ssh_exec(cli, f"md5sum {IMAGES_DIR}/{name}")
                if not out or out.split()[0] != local_md5:
                    fail(f"md5 mismatch after push ({name})!")
                    return False
                installed_screens_count += 1
            show_device_screen(cli, "operation-done")
            ok(f"installed {installed_screens_count} screen/spinner file(s) ({', '.join(want_screens)})")

    if installed_boot:
        if confirm and menu("Preview boot animation on device now?", ["Yes", "No"]) == 0:
            startup_preview(cli, num, delay_us)
        info("boot animation plays on next startup")
    if installed_screens_count:
        info("power/restart screens apply immediately; spinners apply on next UI reload")

    return True


def startup_install(cli, zip_path, confirm=True, parts=None):
    info_bundle = read_bundle(zip_path)
    if not info_bundle:
        fail(f"failed to read bundle: {zip_path}")
        return False
    return bundle_install(cli, info_bundle, chosen_parts=parts, confirm=confirm)


def images_install(cli, zip_path, only=None, confirm=True):
    info_bundle = read_bundle(zip_path)
    if not info_bundle:
        fail(f"failed to read bundle: {zip_path}")
        return False
    parts = only if only is not None else [p for p in info_bundle["available_parts"] if p != "Boot animation"]
    return bundle_install(cli, info_bundle, chosen_parts=parts, confirm=confirm)


def pick_bundle_source():
    STARTUP_LOCAL.mkdir(exist_ok=True)
    zips = sorted(STARTUP_LOCAL.glob("*.zip"))
    opts = []
    if BUNDLED_STARTUP_ZIP.exists():
        opts.append(f"Bundled outblindstop ({BUNDLED_STARTUP_ZIP.name})")
    for z in zips:
        if BUNDLED_STARTUP_ZIP.exists() and z.resolve() == BUNDLED_STARTUP_ZIP.resolve():
            continue
        opts.append(z.name)
    if BUNDLED_SCREENS_DIR.exists():
        opts.append(f"Loose folder ({BUNDLED_SCREENS_DIR.name})")
    opts += ["Type a custom path", "Back"]

    sel = menu("Select bundle to install", opts, info_lines=[
        f"{len(zips)} zip bundle(s) available in {STARTUP_LOCAL}/",
        f"{DIM}Bundles can contain boot animations, power-off screens, restart, and spinners.{RESET}",
    ])
    if sel == -1 or opts[sel] == "Back":
        return None

    choice = opts[sel]
    if choice.startswith("Bundled outblindstop"):
        return BUNDLED_STARTUP_ZIP
    if choice.startswith("Loose folder"):
        return BUNDLED_SCREENS_DIR
    if choice == "Type a custom path":
        p = prompt("bundle zip or folder path:")
        if not p:
            return None
        p = Path(p)
        if not p.exists():
            fail(f"{p} not found")
            return None
        return p

    for z in zips:
        if z.name == choice:
            return z
    return None


def flow_bundle_install(cli):
    src = pick_bundle_source()
    if not src:
        return
    info_bundle = read_bundle(src)
    if not info_bundle:
        fail(f"could not read bundle from {src}")
        return
    bundle_install(cli, info_bundle, confirm=True)


def flow_images_install(cli):
    flow_bundle_install(cli)


def startup_pick_zip():
    return pick_bundle_source()


def flow_root_startup_offer(cli):
    target_bundle = None
    if BUNDLED_STARTUP_ZIP.exists():
        target_bundle = BUNDLED_STARTUP_ZIP
    elif BUNDLED_SCREENS_DIR.exists():
        target_bundle = BUNDLED_SCREENS_DIR
    else:
        return cli

    bundle_info = read_bundle(target_bundle)
    if not bundle_info:
        return cli

    num, delay, _ = startup_params(cli)
    lines = [
        f"device now: {num} frames, {delay / 1000:.1f} ms/frame",
        f"bundle:     {target_bundle.name} ({', '.join(bundle_info['available_parts'])})",
        "stock branding is backed up first so you can easily revert",
    ]
    sel = menu("Install the outblindstop look?", [
        "Yes (backup stock + install)",
        "No (keep stock)",
    ], info_lines=lines)
    if sel != 0:
        return cli

    cli = ssh_ensure(cli)
    if not cli:
        return None

    if not list(STARTUP_LOCAL.glob("animation-backup-*.zip")):
        info("backing up stock branding first...")
        if not startup_backup(cli):
            warn("backup failed, skipping install")
            return cli
    else:
        info("a stock backup already exists, skipping re-backup")

    cli = ssh_ensure(cli)
    if not cli:
        return None

    if bundle_install(cli, bundle_info, confirm=False):
        ok("outblindstop bundle installed successfully!")
    info("to revert or change later: Tweaks > Startup & animations > Install bundle")
    return cli


def startup_preview(cli, num=None, delay=None):
    if not num:
        num, delay, _ = startup_params(cli)
    ssh_exec(cli, "killall -q mifi_display_png")
    rc, out, err = ssh_exec(
        cli, f"sh {STARTUP_DIR}/mifi_display_animation.sh start", timeout=60)
    if rc != 0:
        fail(f"preview failed: {out or err}")
        return
    ok("playing boot animation on the device now")
    info("(the device UI redraws over it when complete)")


def video_to_startup_animation(video_path):
    video = Path(video_path)
    if not video.exists():
        fail(f"{video} not found")
        return None
    if not shutil.which("ffmpeg"):
        fail("ffmpeg not found on this PC (winget install ffmpeg)")
        return None
    info(f"converting {video.name} ...")
    fps_target = 1_000_000 / STOCK_DELAY_US
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
        capture_output=True, text=True)
    try:
        duration = float(r.stdout.strip())
    except ValueError:
        duration = 0.0
    if duration > 0:
        n_expected = max(2, round(duration * fps_target))
        fps = n_expected / duration
        delay = round(duration * 1_000_000 / n_expected)
    else:
        fps, delay = fps_target, STOCK_DELAY_US

    STARTUP_LOCAL.mkdir(exist_ok=True)
    zip_path = STARTUP_LOCAL / f"{video.stem}.zip"
    with tempfile.TemporaryDirectory() as tmp:
        vf = ("scale=320:240:force_original_aspect_ratio=decrease,"
              "pad=320:240:(ow-iw)/2:(oh-ih)/2:color=black,"
              f"fps={fps:.6f}")
        r = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", str(video), "-vf", vf,
             "-pix_fmt", "rgb24", str(Path(tmp) / "animation_%d.png")],
            capture_output=True, text=True)
        if r.returncode != 0:
            fail(f"ffmpeg failed: {r.stderr[-400:]}")
            return None
        frames = sorted(Path(tmp).glob("animation_*.png"),
                        key=lambda p: _frame_num(p.name))
        if not frames:
            fail("ffmpeg produced no frames")
            return None

        extra = []
        if BUNDLED_SCREENS_DIR.exists():
            for f in sorted(BUNDLED_SCREENS_DIR.iterdir()):
                if f.is_file() and f.suffix.lower() in (".png", ".gif"):
                    extra.append((f.name, f.read_bytes()))

        script_text = (ANIM_SH.replace("__NUM_FILES__", str(len(frames)))
                              .replace("__USLEEP__", str(delay)))

        meta = {
            "format": "mifi-unified-bundle",
            "num_frames": len(frames),
            "delay_us": int(delay),
            "resolution": f"{ANIM_W}x{ANIM_H}",
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "origin": f"converted from {video}",
            "images": [a for a, _ in extra],
        }
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("metadata.json", json.dumps(meta, indent=2))
            for f in frames:
                zf.write(f, f"animation_{_frame_num(f.name)}.png")
            zf.writestr("mifi_display_animation.sh", script_text)
            for arcname, data in extra:
                zf.writestr(f"images/{arcname}", data)

    ok(f"{len(frames)} frames @ {delay / 1000:.1f} ms/frame "
       f"({1_000_000 / delay:.2f} fps), {len(frames) * delay / 1e6:.2f} s total"
       + (f" + {len(extra)} UI screen(s)" if extra else ""))
    ok(f"packed bundle -> {zip_path}")
    return zip_path


def flow_startup_video(cli):
    video = prompt("video path:", r"C:\Users\Josh\Downloads\outblindstop startup.mp4")
    if not video:
        return
    zip_path = video_to_startup_animation(video)
    if not zip_path:
        pause()
        return
    if menu("Install bundle on the device now?", ["Not yet", "Install"]) == 1:
        info_bundle = read_bundle(zip_path)
        if info_bundle:
            bundle_install(cli, info_bundle, confirm=True)


def flow_add_images_to_bundle(cli=None):
    STARTUP_LOCAL.mkdir(exist_ok=True)
    zips = sorted(STARTUP_LOCAL.glob("*.zip"))
    opts = []
    if BUNDLED_STARTUP_ZIP.exists():
        opts.append(f"Bundled outblindstop ({BUNDLED_STARTUP_ZIP.name})")
    for z in zips:
        if BUNDLED_STARTUP_ZIP.exists() and z.resolve() == BUNDLED_STARTUP_ZIP.resolve():
            continue
        opts.append(z.name)
    opts += ["Create a new empty bundle zip", "Type a custom path", "Back"]

    sel = menu("Select bundle to add images to", opts, info_lines=[
        "Add power-off, restart, reset screens, or spinners into a bundle zip",
    ])
    if sel == -1 or opts[sel] == "Back":
        return

    choice = opts[sel]
    if choice.startswith("Bundled outblindstop"):
        target_zip = BUNDLED_STARTUP_ZIP
    elif choice == "Create a new empty bundle zip":
        name = prompt("New bundle name (without .zip):", "my-custom-look")
        if not name:
            return
        if not name.lower().endswith(".zip"):
            name += ".zip"
        target_zip = STARTUP_LOCAL / name
    elif choice == "Type a custom path":
        p = prompt("Bundle zip path:")
        if not p:
            return
        target_zip = Path(p)
    else:
        target_zip = next(z for z in zips if z.name == choice)

    # Offer to pick source of images
    src_opts = []
    if BUNDLED_SCREENS_DIR.exists():
        src_opts.append(f"Folder: {BUNDLED_SCREENS_DIR.name} ({BUNDLED_SCREENS_DIR})")
    src_opts += ["Pick a specific image file", "Pick a folder of images", "Back"]

    src_sel = menu(f"Where are the images to add to {target_zip.name}?", src_opts)
    if src_sel == -1 or src_opts[src_sel] == "Back":
        return

    src_choice = src_opts[src_sel]
    images_to_add = [] # (arcname, data)

    if src_choice.startswith("Folder:"):
        for f in sorted(BUNDLED_SCREENS_DIR.iterdir()):
            if f.is_file() and f.suffix.lower() in (".png", ".gif"):
                images_to_add.append((f.name, f.read_bytes()))
    elif src_choice == "Pick a folder of images":
        p = prompt("Folder path with images:")
        if not p:
            return
        fp = Path(p)
        if not fp.exists() or not fp.is_dir():
            fail(f"{fp} is not a valid folder")
            return
        for f in sorted(fp.iterdir()):
            if f.is_file() and f.suffix.lower() in (".png", ".gif"):
                images_to_add.append((f.name, f.read_bytes()))
    elif src_choice == "Pick a specific image file":
        p = prompt("Image file path (.png or .gif):")
        if not p:
            return
        fp = Path(p)
        if not fp.exists() or not fp.is_file():
            fail(f"{fp} not found")
            return

        # Ask what device screen this represents
        preset_names = [
            "poweroff.png (Shutdown / power off screen)",
            "restart.png (Restart / reboot screen)",
            "reset.png (Factory reset screen)",
            "activity-spinner.gif (Loading spinner)",
            "Custom filename",
        ]
        preset_sel = menu(f"What type of screen is {fp.name}?", preset_names)
        if preset_sel == -1:
            return
        if preset_sel == 0:
            target_name = "poweroff.png"
        elif preset_sel == 1:
            target_name = "restart.png"
        elif preset_sel == 2:
            target_name = "reset.png"
        elif preset_sel == 3:
            target_name = "activity-spinner.gif"
        else:
            target_name = prompt("Filename inside images/ in bundle:", fp.name) or fp.name

        images_to_add.append((target_name, fp.read_bytes()))

    if not images_to_add:
        warn("no valid .png or .gif images found to add")
        return

    # Add images into the zip
    target_zip.parent.mkdir(parents=True, exist_ok=True)
    existing_entries = {}
    if target_zip.exists():
        try:
            with zipfile.ZipFile(target_zip, "r") as zf:
                existing_entries = {name: zf.read(name) for name in zf.namelist()}
        except Exception as e:
            fail(f"could not open existing zip: {e}")
            return

    try:
        meta = json.loads(existing_entries.get("metadata.json", b"{}").decode("utf-8"))
    except Exception:
        meta = {}

    current_images = set(meta.get("images", []))
    for arcname, data in images_to_add:
        dest_arc = f"images/{arcname}"
        existing_entries[dest_arc] = data
        current_images.add(arcname)
        info(f"  + images/{arcname} ({len(data)} bytes)")

    meta["images"] = sorted(current_images)
    if "format" not in meta:
        meta["format"] = "mifi-unified-bundle"
    if "created" not in meta:
        meta["created"] = time.strftime("%Y-%m-%d %H:%M:%S")

    existing_entries["metadata.json"] = json.dumps(meta, indent=2).encode("utf-8")

    with zipfile.ZipFile(target_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in existing_entries.items():
            zf.writestr(name, data)

    ok(f"successfully added {len(images_to_add)} image(s) to {target_zip.name}!")
    info(f"images in bundle: {', '.join(sorted(current_images))}")

    if cli and menu("Install this bundle on the device now?", ["Not now", "Install"]) == 1:
        info_bundle = read_bundle(target_zip)
        if info_bundle:
            bundle_install(cli, info_bundle, confirm=True)


def flow_startup_animation(cli):
    while True:
        cli = ssh_ensure(cli)
        if not cli:
            return
        num, delay, _ = startup_params(cli)
        zips = sorted(STARTUP_LOCAL.glob("*.zip")) if STARTUP_LOCAL.exists() else []
        sel = menu("Startup & animations", [
            "Backup device look (boot animation + all screens + spinners)",
            "Install bundle (pick parts: boot, shutdown, restart, etc.)",
            "Add images to a bundle (shutdown, restart, reset, spinners)",
            "Create animation bundle from video (ffmpeg)",
            "Preview current boot animation on device",
            "Back",
        ], info_lines=[
            f"device: {num} boot frames, {delay / 1000:.1f} ms/frame ({num * delay / 1e6:.2f} s)",
            f"bundles: {len(zips)} zip(s) in {STARTUP_LOCAL}/",
        ])
        if sel in (-1, 5):
            return
        clear()
        banner()
        print()
        if sel == 0:
            startup_backup(cli)
        elif sel == 1:
            flow_bundle_install(cli)
        elif sel == 2:
            flow_add_images_to_bundle(cli)
        elif sel == 3:
            flow_startup_video(cli)
        elif sel == 4:
            startup_preview(cli)
        pause()
