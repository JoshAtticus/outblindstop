import hashlib
import os
import re
import socket
from pathlib import Path

import requests

from .constants import BASE, HOST, PAYLOAD_PORT


def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((HOST, 80))
        return s.getsockname()[0]
    finally:
        s.close()


def check_server():
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
