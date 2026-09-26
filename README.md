# Inseego MiFi M2xxx / M3xxx root tool

by JoshAtticus

Roots Inseego MiFi hotspots (M2000, M3100, M3200 and friends) through the OpenVPN
client that ships in the WebUI. Tested on an M3200 (Telstra, SDX65). Other
M2xxx / M3xxx devices should work but are untested.

Use this on your own device. Carrier locks, warranty and radio regulations
still apply, and IMEI changes may be illegal where you live.

## How it works

The WebUI lets you upload an OpenVPN client config, which is fed to the
device's own OpenVPN client running as root, without sanitising the
`tls-verify` directive. OpenVPN does not run `tls-verify` through a shell (the
command is split on spaces into argv), so chaining with `;` does not work.
The exploit therefore runs in two stages, driven through the WebUI's own VPN
endpoints:

1. Stage 1: `tls-verify` runs curl, downloading the payload from your PC to
   `/tmp/root.sh`
2. Stage 2: `tls-verify` runs `/bin/sh /tmp/root.sh`

The payload sets a known root password (`Root@123`) and starts dropbear on
port 2222 (telnet on 2323 as a fallback). `tls-verify` fires during the TLS
handshake, before user/password auth, so the VPN server and its credentials do
not need to be valid. The device does need internet access (cellular data) to
reach the VPN server.

## Quick start

1. Connect to the hotspot's WiFi
2. `pip install paramiko requests pillow`
3. `python3 mifi_tool.py`

The tool checks for the WebUI (it answers with a `Server: MiFi` header), picks
a base config from `ovpn/`, asks for the admin password (the default is the
default WiFi password, also visible in WebUI > Settings > Advanced > Advanced)
and drives both stages automatically. It then offers to install persistence
(ssh survives reboots) and shows a success screen on the device display. The
"Yay!" button on that screen is positioned over the connected devices button
of the device UI, so tapping it opens the menu through the touch layer.

Menu overview:

* **Rooting**: run the exploit, install or uninstall persistence, test access
* **Backup**: dumps every MTD partition over SSH. Do this before changing
  anything, these dumps are the only recovery path (no EDL firehose exists)
* **Tweaks**: modem band and carrier aggregation control via `modem2_cli`
* **SSH shell**: root shell over ssh, with the do not do this list

## Manual method (no tool)

1. Serve the repo root: `python3 -m http.server 8000`
2. Take any `.ovpn` the device accepts (NordVPN TCP configs work, credentials
   do not matter) and append the exploit lines from
   `payload/exploit-additions.ovpn` (stage 1 variant: the curl line). The
   `tls-verify` line MUST be indented with a literal TAB (0x09)
3. Upload it in the WebUI (Settings > Advanced > VPN) and hit Connect. Your
   HTTP server log should show a GET for `/payload/root.sh`
4. Clear all OpenVPN settings in the WebUI, upload a stage 2 variant (the
   `/bin/sh /tmp/root.sh` line), Connect again
5. `ssh -p 2222 root@192.168.1.1` (password `Root@123`)

## Warnings

* Never flash or erase `boot`, `abl`, `xbl`, `sbl` or `tz` over fastboot.
  There is no signed EDL firehose for these devices, so a bad bootloader flash
  is an unrecoverable brick
* Never restore a `/persist` dump from another device. TrustZone encrypts the
  WebUI passwords per device, a foreign dump crashes the WebUI
* Fastboot flashing of `system` requires a ubinized UBI image, not a raw dd of
  the mtdblock
* Do not cat binary files into your terminal session

## Repo layout

* `mifi_tool.py`: the all in one tool (root, backup, tweaks, shell)
* `payload/root.sh`: the root payload
* `ovpn/`: base OpenVPN config sample for the exploit
* `assets/success.png`: the post root success screen (tap the Yay! button)
* `modem2_cli_help.txt`: full modem2_cli command list from an M3200
* `tools/`: development helpers used while researching the device
  (`backup.py` adb era partition dumper, `persist.py` persistence installer,
  `fetch_rootfs.py` and `extract.py` firmware extraction, `duck.py` puts a
  spinning duck on the hotspot's screen, obviously)
* `local/`: put your own cert configs here, it is gitignored

## Credits

* bg7dcw and the XDA thread "Inseego M3200 firmware dump and port open" for
  the original discovery
* WetFart1337 for the OpenVPN `tls-verify` injection method on the M2000
* Geofferey, andkov and Renate for persistence, backups and EDL research

