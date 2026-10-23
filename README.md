# outblindstop
<img width="1019" height="276" alt="outblindstop" src="https://github.com/user-attachments/assets/9ac8bac9-df7c-4c01-878b-50562143f6ca" />

Root access & tweaks for inseego M2xxx and M3xxx devices.

Tested on an M3200 (Telstra, SDX65). Other M2xxx / M3xxx devices should work but are untested.

Use this on your own device. This does not remove SIM lock if your device has it, and IMEI changes may be illegal where you live.

## Quick start
1. Connect to the hotspot's WiFi
2. `pip install -r requirements.txt`
3. `python3 mifi_tool.py`

### M2000 SSH compatibility

Some M2000 firmware versions start an older Dropbear SSH server that does not
share a key-exchange algorithm with recent Paramiko releases. The connection
code enables the legacy algorithms only for this device connection. If you
still see `Incompatible ssh peer (no acceptable kex algorithm)`, make sure the
dependencies were installed from `requirements.txt` and run the tool again.

This compatibility change does not unlock a carrier-locked device. It only
allows the temporary root SSH session to be established after the payload has
run.

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
