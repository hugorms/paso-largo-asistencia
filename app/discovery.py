"""Búsqueda de terminales Hikvision en la red local, para cuando la PC o el equipo cambian de red.

Dos métodos, como la herramienta SADP de Hikvision:
- SADP: pregunta por multicast UDP 239.255.255.250:37020 y cada equipo Hikvision responde con su IP,
  número de serie y MAC (funciona aunque el equipo esté en otra subred del mismo switch).
- Barrido: prueba las IP de la subred (/24) de cada tarjeta de red de esta PC y se queda con las que
  responden como ISAPI (piden usuario/clave Digest en /ISAPI/System/deviceInfo).
"""
import ipaddress
import re
import socket
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

import httpx

SADP_GROUP, SADP_PORT = "239.255.255.250", 37020
_lock = threading.Lock()  # una búsqueda a la vez (el lector automático y el botón usan el mismo puerto UDP)


def local_ips():
    """IPv4 de esta PC en sus redes (sin 127.x ni 169.254.x)."""
    ips = set()
    try:
        ips.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no envía nada: solo elige la tarjeta de red principal
        ips.add(s.getsockname()[0])
    except OSError:
        pass
    finally:
        s.close()
    return sorted(ip for ip in ips if not ip.startswith(("127.", "169.254.", "0.")))


def networks():
    """Subredes /24 que se barren (máx. 4 tarjetas de red)."""
    nets = []
    for ip in local_ips():
        net = ipaddress.ip_network(f"{ip}/24", strict=False)
        if net not in nets:
            nets.append(net)
    return nets[:4]


def _mac(m):
    return re.sub(r"[^0-9a-f]", "", (m or "").lower())


def same_device(serial, mac, other_serial, other_mac=None):
    """¿Es el mismo equipo? Se compara el número de serie (o la MAC si falta)."""
    a, b = (serial or "").strip().upper(), (other_serial or "").strip().upper()
    if a and b:
        return a == b or (min(len(a), len(b)) >= 9 and (a.endswith(b) or b.endswith(a)))
    return bool(_mac(mac)) and _mac(mac) == _mac(other_mac)


# ------------------------------------------------------------------ SADP
def _parse_sadp(data):
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None
    for e in root.iter():
        if "}" in e.tag:
            e.tag = e.tag.split("}", 1)[1]
    if root.tag != "ProbeMatch":
        return None  # p. ej. nuestra propia pregunta, que vuelve por el multicast
    g = lambda t: (root.findtext(t) or "").strip()
    ip = g("IPv4Address")
    if not ip:
        return None
    return {"ip": ip, "port": int(g("HttpPort") or 80), "https": False, "serial": g("DeviceSN"),
            "mac": g("MAC"), "model": g("DeviceDescription") or g("DeviceType"), "mask": g("IPv4SubnetMask"),
            "via": "SADP"}


def sadp(timeout=2.5):
    """Equipos Hikvision que responden a la consulta SADP."""
    probe = (f'<?xml version="1.0" encoding="utf-8"?><Probe><Uuid>{str(uuid.uuid4()).upper()}</Uuid>'
             "<Types>inquiry</Types></Probe>").encode()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("", SADP_PORT))  # los equipos responden al grupo multicast en este puerto
    except OSError:
        s.bind(("", 0))  # el puerto lo tiene otro programa (p. ej. la herramienta SADP)
    found = {}
    try:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        for ip in local_ips() or ["0.0.0.0"]:
            try:
                s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                             socket.inet_aton(SADP_GROUP) + socket.inet_aton(ip))
            except OSError:
                pass
            try:
                s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
                s.sendto(probe, (SADP_GROUP, SADP_PORT))
            except OSError:
                pass
        end = time.time() + timeout
        while (left := end - time.time()) > 0:
            s.settimeout(left)
            try:
                data, _ = s.recvfrom(65535)
            except socket.timeout:
                break
            except OSError:
                continue
            d = _parse_sadp(data)
            if d:
                found[d["ip"]] = d
    finally:
        s.close()
    return list(found.values())


# ------------------------------------------------------------------ barrido de la subred
def _port_open(target):
    try:
        with socket.create_connection(target, timeout=0.5):
            return True
    except OSError:
        return False


def _isapi(target):
    """Si en ip:puerto responde un equipo ISAPI devuelve si usa HTTPS; si no, None."""
    host, port = target
    for https in ((True, False) if port != 80 else (False, True)):
        try:
            r = httpx.get(f"{'https' if https else 'http'}://{host}:{port}/ISAPI/System/deviceInfo",
                          timeout=3, verify=False)
        except httpx.HTTPError:
            continue
        auth = r.headers.get("www-authenticate", "").lower()
        if (r.status_code == 401 and ("digest" in auth or "basic" in auth)) or b"DeviceInfo" in r.content:
            return https
    return None


def scan(ports=(80, 443)):
    """Equipos que responden como ISAPI en la subred de esta PC (sin serie: hace falta la clave)."""
    targets = [(str(h), p) for net in networks() for h in net.hosts() for p in sorted(set(ports))]
    with ThreadPoolExecutor(128) as ex:
        open_ = [t for t, ok in zip(targets, ex.map(_port_open, targets)) if ok]
        kinds = list(ex.map(_isapi, open_))
    return [{"ip": h, "port": p, "https": https, "serial": "", "mac": "", "model": "", "via": "red"}
            for (h, p), https in zip(open_, kinds) if https is not None]


def find(ports=(80, 443), with_scan=True):
    """SADP + barrido, una entrada por IP (los datos de SADP mandan)."""
    with _lock:
        found = {d["ip"]: d for d in sadp()}
        if with_scan:
            for d in scan(ports):
                if d["ip"] in found:
                    found[d["ip"]].update(port=d["port"], https=d["https"])  # el puerto web real
                else:
                    found[d["ip"]] = d
    return sorted(found.values(), key=lambda d: tuple(int(x) for x in d["ip"].split(".")))
