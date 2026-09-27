#!/usr/bin/env python3
"""
Inseego MiFi M2xxx / M3xxx all in one root, backup and tweak tool
by JoshAtticus

Tested on an M3200 (Telstra, SDX65). Should be universal across M2xxx/M3xxx
devices but only tested on the M3200.

Requirements: pip install paramiko requests
"""

import os
import sys

import requests

try:
    os.system("")  # enable ANSI escapes on legacy Windows consoles
except Exception:
    pass

from mifi.constants import (
    BASE,
    HOST,
    PAYLOAD_LOCAL,
    PAYLOAD_PORT,
    ROOT_PW,
    SSH_PORT,
    TELNET_PORT,
)
from mifi.ui import (
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
    DIM,
    GREEN,
    RED,
    RESET,
    YELLOW,
)
from mifi.device import (
    ensure_ssh_key,
    probe_root,
    push_bytes,
    show_device_screen,
    ssh_connect,
    ssh_ensure,
    ssh_exec,
)
from mifi.web import (
    check_server,
    device_model,
    get_local_ip,
    vpn_clear,
    vpn_connect,
    vpn_token,
    vpn_upload,
    web_login,
)
from mifi.files import (
    device_list_dir,
    flow_browse_pull,
    flow_files,
    pull_from_device,
    push_to_device,
)
from mifi.root import (
    PayloadServer,
    build_stage_configs,
    find_base_ovpn,
    flow_persistence,
    flow_root,
    flow_unroot,
    persistence_installed,
    root_menu,
)
from mifi.backup import flow_backup
from mifi.startup import (
    flow_add_images_to_bundle,
    flow_bundle_install,
    flow_images_install,
    flow_root_startup_offer,
    flow_startup_animation,
    flow_startup_video,
    images_install,
    read_bundle,
    startup_backup,
    startup_install,
    startup_params,
    startup_pick_zip,
    startup_preview,
    video_to_startup_animation,
)
from mifi.tweaks import (
    duck_install,
    duck_installed,
    duck_running,
    duck_start,
    duck_stop,
    flow_duck,
    flow_ssh_shell,
    modem2_set_nr5g_bands,
    tweak_submenu,
)


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


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        clear()
        print("\nbye\n")
