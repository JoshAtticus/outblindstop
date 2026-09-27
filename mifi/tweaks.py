from pathlib import Path
import subprocess
import time

from .constants import (
    DUCK_SH,
    HOST,
    MODEM2,
    ROOT_PW,
    SSH_PORT,
)
from .device import (
    ensure_ssh_key,
    push_bytes,
    show_device_screen,
    ssh_connect,
    ssh_ensure,
    ssh_exec,
)
from .startup import flow_startup_animation
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
    DIM,
    MAGENTA,
    RED,
    RESET,
    YELLOW,
)


def modem2_set_nr5g_bands(cli, kind, bands):
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
        cmd += ["-i", str(key), "-o", "IdentitiesOnly=yes"]
    cmd.append(f"root@{HOST}")

    def _screen(name):
        try:
            cli = ssh_connect()
            show_device_screen(cli, name)
            cli.close()
        except Exception:
            pass

    _screen("operation-inprogress")
    subprocess.call(cmd)
    _screen("operation-done")


def tweak_submenu(cli):
    while True:
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
        elif sel == 10:
            if menu("Reboot device?", ["Cancel", "Reboot"]) == 1:
                ssh_exec(cli, "reboot")
                ok("rebooting, ssh will drop")
                pause()
                return
        if sel not in (-1, 9, 10, 11):
            pause()
