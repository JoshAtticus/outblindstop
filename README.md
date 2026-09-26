# outblindstop

Root access & tweaks for inseego M2xx and M3xx devices.

Tested on an M3200 (Telstra, SDX65). Other M2xxx / M3xxx devices should work but are untested.

Use this on your own device. This does not remove SIM lock if your device has it, and IMEI changes may be illegal where you live.

## Quick start
1. Connect to the hotspot's WiFi
2. `pip install paramiko requests pillow`
3. `python3 mifi_tool.py`

* **Rooting**: run the exploit, install or uninstall persistence, test access
* **Backup**: dumps every MTD partition over SSH. Do this before changing
  anything, these dumps are the only recovery path (no EDL firehose exists)
* **Tweaks**: modem band and carrier aggregation control via `modem2_cli`, other fun things :3
* **SSH shell**: root shell over ssh

## Warnings

* Never flash or erase `boot`, `abl`, `xbl`, `sbl` or `tz` over fastboot. There is no signed EDL firehose for these devices.
  is an unrecoverable brick
* Never restore a `/persist` dump from another device.
* Fastboot flashing of `system` requires a ubinized UBI image
* Do not cat binary files into your terminal session