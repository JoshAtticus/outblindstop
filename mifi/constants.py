from pathlib import Path

HOST = "192.168.1.1"
BASE = f"http://{HOST}"
SSH_PORT = 2222
TELNET_PORT = 2323
ROOT_PW = "Root@123"
PAYLOAD_PORT = 8000
PAYLOAD_LOCAL = "payload/root.sh"
FRAMEWORK = "M2xxx/M3xxx"

PERSISTENCE_MARKER = "# --- root persistence (added by mifi_tool) ---"
PERSISTENCE_FILES = [
    "/opt/nvtl/bin/init-SDX65.sh",     # SDX65 (M3000/M3100/M3200)
    "/opt/nvtl/bin/init-SDX55.sh",     # SDX55 (M2000)
    "/opt/nvtl/bin/syslogd_monitor.sh",
]

MODEM2 = "LD_LIBRARY_PATH=/opt/nvtl/lib /opt/nvtl/bin/modem2_cli"

STARTUP_DIR = "/opt/nvtl/data/branding/startup"
IMAGES_DIR = "/opt/nvtl/data/branding/deviceui/images"
BRANDING_DIR = "/opt/nvtl/data/branding"
STARTUP_LOCAL = Path("startup")
BUNDLED_STARTUP_ZIP = STARTUP_LOCAL / "outblindstop startup.zip"
BUNDLED_SCREENS_DIR = Path.home() / "Downloads" / "outblindstop off screens"
STOCK_DELAY_US = 18000          # stock: 89 frames, 18 ms/frame (~55.6 fps)
ANIM_W, ANIM_H = 320, 240

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

IMAGE_GROUPS = [
    ("Power-off screen", "DeviceImages_DeviceUI_MIFIScreen_OFF.png"),
    ("Restart screen", "DeviceImages_DeviceUI_MIFIScreen_Restart.png"),
    ("Reset screen", "DeviceImages_DeviceUI_MIFIScreen_Reset.png"),
    ("Loading spinners", "spinner"),
]

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

OVPN_SEARCH = [
    "ovpn",                                            # bundled NordVPN sample
    "local",                                          # your own configs (gitignored)
    ".",                                              # repo-local configs
]
