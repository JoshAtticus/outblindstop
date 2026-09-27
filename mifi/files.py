import hashlib
from pathlib import Path, PurePosixPath
import shlex
import tarfile
import tempfile

from .device import show_device_screen, ssh_connect, ssh_exec, probe_root
from .ui import (
    banner,
    clear,
    fail,
    menu,
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


def pull_from_device(cli, remote_path, out_dir="pulled"):
    remote_path = remote_path.strip()
    expected = remote_path.strip("/")
    name = expected.replace("/", "_") or "root"
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tarball = out_dir / f"{name}.tar.gz"

    show_device_screen(cli, "operation-inprogress")
    print(f"  {CYAN}streaming {remote_path} -> {tarball}{RESET}")
    _, stdout, _ = cli.exec_command(f"tar czf - {shlex.quote(remote_path)}")
    with open(tarball, "wb") as f:
        while True:
            chunk = stdout.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
    rc = stdout.channel.recv_exit_status()
    if rc != 0 or tarball.stat().st_size == 0:
        fail(f"tar failed on device (exit {rc}) - does the path exist?")
        return None

    dest = out_dir / name
    skipped = 0

    def _pull_filter(member, path):
        nonlocal skipped
        if member.issym() or member.islnk() or member.ischr() \
                or member.isblk() or member.isfifo():
            skipped += 1
            return None
        return tarfile.data_filter(member, path)

    with tarfile.open(tarball, "r:gz") as tf:
        members = tf.getmembers()
        for m in members:
            if not (m.name == expected or m.name.startswith(expected + "/")):
                fail(f"unexpected path in tar, refusing to extract: {m.name}")
                return None
        try:
            tf.extractall(dest, filter=_pull_filter)
        except TypeError:
            tf.extractall(dest)

    files = [m for m in members if m.isfile()]
    ok(f"pulled {len(files)} files -> {dest}"
       + (f" ({skipped} symlinks/dev nodes skipped)" if skipped else ""))
    for m in sorted(files, key=lambda m: m.name):
        print(f"    {m.name}  ({m.size} B)")
    show_device_screen(cli, "operation-done")
    return dest


def push_to_device(cli, local_path, remote_path, make_exec=False):
    local = Path(local_path)
    remote_path = remote_path.rstrip("/")
    base = str(PurePosixPath(remote_path).parent)
    arcname = PurePosixPath(remote_path).name if local.is_file() else (
        PurePosixPath(remote_path).name or local.name)

    show_device_screen(cli, "operation-inprogress")
    cmd = f"mkdir -p {shlex.quote(base)} && tar xzf - -C {shlex.quote(base)}"
    with tempfile.TemporaryDirectory() as tmp:
        tarball = Path(tmp) / "push.tar.gz"
        with tarfile.open(tarball, "w:gz") as tf:
            tf.add(local, arcname=arcname)
            want = sum(1 for m in tf.getmembers() if m.isfile())
        chan = cli.get_transport().open_session()
        chan.exec_command(cmd)
        with open(tarball, "rb") as f:
            while True:
                chunk = f.read(1 << 16)
                if not chunk:
                    break
                chan.sendall(chunk)
        chan.shutdown_write()
        rc = chan.recv_exit_status()
    if rc != 0:
        fail(f"push failed on device (exit {rc})")
        return False

    if local.is_file():
        _, out, _ = ssh_exec(cli, f"md5sum {shlex.quote(remote_path)}")
        local_md5 = hashlib.md5(local.read_bytes()).hexdigest()
        if out.split()[0] != local_md5:
            fail("md5 mismatch after push!")
            return False
    else:
        _, out, _ = ssh_exec(cli, f"find {shlex.quote(remote_path)} -type f | wc -l")
        if int(out or -1) != want:
            fail(f"file count mismatch after push (device {out}, local {want})")
            return False
    if make_exec:
        ssh_exec(cli, f"chmod +x {shlex.quote(remote_path)}")
    ok(f"pushed {local} -> {remote_path} (verified)")
    show_device_screen(cli, "operation-done")
    return True


def device_list_dir(cli, path):
    rc, out, err = ssh_exec(cli, f"ls -A1p {shlex.quote(path)} 2>/dev/null")
    if rc != 0:
        return None
    entries = []
    for line in out.splitlines():
        if not line:
            continue
        if line.endswith("/"):
            entries.append((line.rstrip("/"), True))
        else:
            entries.append((line, False))
    return sorted(entries, key=lambda e: (not e[1], e[0].lower()))


def flow_browse_pull(cli, out_dir="pulled"):
    start = prompt("start browsing at:", "/") or "/"
    path = start
    while True:
        entries = device_list_dir(cli, path)
        if entries is None:
            fail(f"can't list {path}, jumping back to {start}")
            pause()
            path = start
            continue
        opts = [f"Pull this whole folder ({path})", ".. (up)"]
        opts += [n + ("/" if d else "") for n, d in entries]
        sel = menu("Files: browse", opts, info_lines=[
            f"device path: {BOLD}{path}{RESET}",
            f"{DIM}enter on a folder = open it, enter on a file = pull it{RESET}",
            f"{DIM}pulls land in {out_dir}/{RESET}",
        ])
        if sel == -1:
            return
        if sel == 0:
            pull_from_device(cli, path, out_dir)
            pause()
            continue
        if sel == 1:
            if path.rstrip("/") == "":
                continue
            parent = str(PurePosixPath(path).parent)
            path = parent if parent != "" else "/"
            continue
        name, is_dir = entries[sel - 2]
        full = path.rstrip("/") + "/" + name
        if is_dir:
            path = full
            continue
        clear()
        banner()
        print()
        pull_from_device(cli, full, out_dir)
        pause()


def flow_files():
    clear()
    banner()
    print(f"  {BOLD}{MAGENTA}Files (push / pull over ssh){RESET}\n")
    up, authed = probe_root()
    if not authed:
        fail("root required (run Rooting first)")
        pause()
        return
    try:
        cli = ssh_connect()
    except Exception as e:
        fail(f"ssh failed: {e}")
        pause()
        return
    try:
        while True:
            sel = menu("Files", [
                "Browse device & pull (file browser)",
                "Pull a path (type it in)",
                "Push file/folder to device",
                "Back",
            ], info_lines=[
                f"{DIM}pulls land in pulled/ (device path becomes the subfolder).{RESET}",
                f"{DIM}pushes stream a tar into the device, md5-verified for files.{RESET}",
            ])
            if sel in (-1, 3):
                return
            clear()
            banner()
            print()
            if sel == 0:
                flow_browse_pull(cli)
            elif sel == 1:
                remote = prompt("remote path to pull:", "/WEBSERVER")
                if not remote:
                    continue
                out = prompt("local folder:", "pulled")
                pull_from_device(cli, remote, out)
            elif sel == 2:
                local = prompt("local file/folder to push:")
                if not local or not Path(local).exists():
                    fail(f"{local or '(empty)'} not found")
                    pause()
                    continue
                remote = prompt("remote destination path (full path):")
                if not remote:
                    continue
                warn(f"this overwrites {remote} on the device.")
                warn("init scripts are fine (restore from .pre-root), but never")
                warn("write into the flash partitions - those are mtd, not files.")
                if menu("Push now?", ["Cancel", "Push"]) != 1:
                    continue
                make_exec = menu("Make it executable (chmod +x)?",
                                 ["No", "Yes (for scripts)"]) == 1
                push_to_device(cli, local, remote, make_exec=make_exec)
            pause()
    finally:
        cli.close()
