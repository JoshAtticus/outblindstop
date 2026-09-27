import io
import socket
import subprocess
import time
from pathlib import Path

import paramiko

from .constants import HOST, ROOT_PW, SSH_PORT
from .ui import fail, ok, warn


def probe_root():
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
    chan = cli.get_transport().open_session()
    chan.exec_command(f"cat > {path}")
    chan.sendall(data)
    chan.shutdown_write()
    return chan.recv_exit_status()


def ensure_ssh_key():
    ssh_dir = Path.home() / ".ssh"
    ssh_dir.mkdir(exist_ok=True)
    key = ssh_dir / "mifi_rsa"
    if not key.exists():
        subprocess.run(["ssh-keygen", "-t", "rsa", "-b", "2048", "-N", "",
                        "-f", str(key), "-q"], check=True)
        ok("generated a new ssh key for this device")
    pub_file = key.with_suffix(".pub")
    if not pub_file.exists():
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


def show_device_screen(cli, name):
    src = Path("assets") / f"{name}.png"
    if not src.exists():
        warn(f"{src} missing, skipping the {name} screen")
        return
    data = src.read_bytes()
    if data[25] == 6:  # RGBA. mifi_display_png only accepts RGB
        try:
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
