"""Pull /WEBSERVER (the modem's web UI) to the PC over SSH using paramiko.

Usage: python3 pull_web.py

Streams a gzipped tar of /WEBSERVER directly to the PC (device /tmp is tiny),
then unpacks it to ./webserver/ so the files can be edited locally.
"""

import getpass
import os
import sys
import tarfile

import paramiko

OUT_DIR = "webserver"
HOST = "192.168.1.1"
PORT = 2222
USER = "root"
REMOTE_PATH = "/WEBSERVER"


def connect(password):
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        cli.connect(HOST, port=PORT, username=USER, password=password,
                    look_for_keys=False, allow_agent=False, timeout=15)
        return cli
    except paramiko.ssh_exception.SSHException:
        # old dropbear: disable rsa-sha2 so only legacy ssh-rsa is offered
        transport = paramiko.Transport((HOST, PORT))
        transport.disabled_algorithms = {"pubkeys": ["rsa-sha2-256", "rsa-sha2-512"]}
        transport.start_client(timeout=15)
        if not transport.is_verified():
            raise SystemExit("host key negotiation failed")
        transport.auth_password(USER, password)
        cli._transport = transport
        return cli


def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    password = getpass.getpass(f"{USER}@{HOST} password: ")
    cli = connect(password)

    tarball = os.path.join(OUT_DIR, "WEBSERVER.tar.gz")
    print(f"streaming {REMOTE_PATH} -> {tarball}")
    _, stdout, _ = cli.exec_command(f"tar czf - -C / {REMOTE_PATH.lstrip('/')}")
    written = 0
    with open(tarball, "wb") as f:
        while True:
            chunk = stdout.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
            written += len(chunk)
    rc = stdout.channel.recv_exit_status()
    if rc != 0 or written == 0:
        sys.exit(f"tar failed on device: exit={rc}, got {written} bytes")
    print(f"got {written} bytes\n")

    print(f"unpacking -> ./{OUT_DIR}/")
    with tarfile.open(tarball, "r:gz") as tf:
        members = tf.getmembers()
        # guard against path traversal before extracting
        for m in members:
            if not (m.name == "WEBSERVER" or m.name.startswith("WEBSERVER/")):
                sys.exit(f"unexpected path in tar: {m.name}")
        tf.extractall(OUT_DIR)

    files = [m for m in members if m.isfile()]
    print(f"{len(files)} files, {len(members) - len(files)} dirs")
    for m in sorted(files, key=lambda m: m.name):
        print(f"  {m.name}  ({m.size} B)")

    print(f"\nedit files in ./{OUT_DIR}/WEBSERVER/www/, then repack+push back to the device.")


if __name__ == "__main__":
    main()
