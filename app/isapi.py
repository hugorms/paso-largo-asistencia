"""Cliente ISAPI para terminales de reconocimiento facial Hikvision (serie DS-K1T / MinMoe).

Todas las llamadas usan autenticación Digest, que es la que exigen estos equipos.
"""
import json
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime

import httpx

# Códigos "minor" de eventos de control de acceso (major=5) que indican verificación
# fallida. Un evento con número de empleado y cuyo minor no esté aquí cuenta como marcación.
MINOR_FAIL = {6, 7, 9, 10, 11, 12, 13, 14, 39, 76}
MINOR_NAMES = {
    1: "Tarjeta válida", 6: "Tarjeta inválida", 9: "Sin permiso", 21: "Puerta abierta",
    22: "Puerta cerrada", 38: "Huella válida", 39: "Huella no válida", 75: "Rostro válido",
    76: "Rostro no válido", 104: "Rostro + tarjeta", 1024: "Apertura remota",
}


class ISAPIError(Exception):
    pass


class ISAPIConnError(ISAPIError):
    """El equipo no responde en esa IP (apagado, desconectado o cambió de IP)."""


class ISAPIClient:
    def __init__(self, host, user, password, port=80, https=False, timeout=20.0):
        scheme = "https" if https else "http"
        default = 443 if https else 80
        self.base = f"{scheme}://{host}" + ("" if int(port) == default else f":{port}")
        self.auth = httpx.DigestAuth(user, password)
        self.timeout = httpx.Timeout(timeout, connect=5.0)  # si no está en la red se sabe rápido
        self.verify = False  # los equipos usan certificados autofirmados

    # ------------------------------------------------------------ helpers
    def _req(self, method, path, **kw):
        try:
            with httpx.Client(auth=self.auth, timeout=self.timeout, verify=self.verify) as c:
                r = c.request(method, self.base + path, **kw)
        except httpx.TimeoutException as e:
            raise ISAPIConnError("El equipo no respondió a tiempo (¿apagado o fuera de la red?)") from e
        except httpx.ConnectError as e:
            raise ISAPIConnError("No se pudo conectar con el equipo (¿apagado, desconectado o IP equivocada?)") from e
        except httpx.HTTPError as e:
            raise ISAPIConnError(f"No se pudo conectar con el equipo: {e}") from e
        if r.status_code == 401:
            raise ISAPIError("Usuario o contraseña del equipo incorrectos (401)")
        if r.status_code >= 400:
            raise ISAPIError(f"El equipo respondió {r.status_code}: {_error_text(r)}")
        return r

    def _json(self, method, path, body=None, **kw):
        r = self._req(method, path, content=json.dumps(body) if body is not None else None,
                      headers={"Content-Type": "application/json"}, **kw)
        data = r.json() if r.content else {}
        # Algunos firmwares devuelven 200 con un statusCode de error
        if isinstance(data, dict) and data.get("statusCode") not in (None, 1) and "errorMsg" in data:
            raise ISAPIError(f"{data.get('errorMsg')} ({data.get('subStatusCode')})")
        return data

    def _xml(self, method, path, body=None):
        r = self._req(method, path, content=body, headers={"Content-Type": "application/xml"})
        return _strip_ns(ET.fromstring(r.content)) if r.content else None

    # ------------------------------------------------------------ sistema
    def device_info(self):
        root = self._xml("GET", "/ISAPI/System/deviceInfo")
        return {child.tag: (child.text or "").strip() for child in root}

    def get_time(self):
        root = self._xml("GET", "/ISAPI/System/time")
        return {child.tag: (child.text or "").strip() for child in root}

    def set_time(self, local_dt: datetime, tz_iso_offset: str):
        """Sincroniza el reloj del equipo con la hora del servidor."""
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Time version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">'
            "<timeMode>manual</timeMode>"
            f"<localTime>{local_dt.strftime('%Y-%m-%dT%H:%M:%S')}{tz_iso_offset}</localTime>"
            f"<timeZone>{'CST' + ('+' if tz_iso_offset.startswith('-') else '-') + str(int(tz_iso_offset[1:3])) + ':' + tz_iso_offset[4:6] + ':00'}</timeZone>"
            "</Time>"
        )
        self._xml("PUT", "/ISAPI/System/time", body)

    def reboot(self):
        # El equipo suele reiniciarse antes de responder: un timeout aquí es normal
        try:
            self._xml("PUT", "/ISAPI/System/reboot")
        except ISAPIError as e:
            if "timed out" not in str(e).lower() and "conectar" not in str(e):
                raise

    # ------------------------------------------------------------ usuarios
    def user_count(self):
        d = self._json("GET", "/ISAPI/AccessControl/UserInfo/Count?format=json")
        return int(d.get("UserInfoCount", {}).get("userNumber", 0))

    def search_users(self, employee_no=None):
        users, pos, sid = [], 0, uuid.uuid4().hex[:16]
        while True:
            cond = {"searchID": sid, "searchResultPosition": pos, "maxResults": 30}
            if employee_no:
                cond["EmployeeNoList"] = [{"employeeNo": str(employee_no)}]
            d = self._json("POST", "/ISAPI/AccessControl/UserInfo/Search?format=json",
                           {"UserInfoSearchCond": cond})
            s = d.get("UserInfoSearch", {})
            batch = s.get("UserInfo", []) or []
            users.extend(batch)
            pos += len(batch)
            if s.get("responseStatusStrg") != "MORE" or not batch:
                return users

    def _user_body(self, employee_no, name, gender="unknown", begin=None, end=None, door_no=1):
        return {"UserInfo": {
            "employeeNo": str(employee_no),
            "name": name[:32],
            "userType": "normal",
            "gender": gender,
            "Valid": {
                "enable": True,
                "beginTime": begin or "2020-01-01T00:00:00",
                "endTime": end or "2037-12-31T23:59:59",
                "timeType": "local",
            },
            "doorRight": str(door_no),
            "RightPlan": [{"doorNo": door_no, "planTemplateNo": "1"}],
        }}

    def upsert_user(self, employee_no, name, **kw):
        body = self._user_body(employee_no, name, **kw)
        if self.search_users(employee_no):
            self._json("PUT", "/ISAPI/AccessControl/UserInfo/Modify?format=json", body)
        else:
            self._json("POST", "/ISAPI/AccessControl/UserInfo/Record?format=json", body)

    def delete_user(self, employee_no):
        self._json("PUT", "/ISAPI/AccessControl/UserInfo/Delete?format=json",
                   {"UserInfoDelCond": {"EmployeeNoList": [{"employeeNo": str(employee_no)}]}})

    # ------------------------------------------------------------ rostros
    def upload_face(self, employee_no, jpeg: bytes):
        meta = json.dumps({"faceLibType": "blackFD", "FDID": "1", "FPID": str(employee_no)})
        files = {"FaceDataRecord": (None, meta, "application/json"),
                 "img": ("face.jpg", jpeg, "image/jpeg")}
        # FDSetUp hace "insertar o reemplazar"; si el firmware no lo soporta se usa FaceDataRecord
        try:
            r = self._req("PUT", "/ISAPI/Intelligent/FDLib/FDSetUp?format=json", files=files)
        except ISAPIError:
            r = self._req("POST", "/ISAPI/Intelligent/FDLib/FaceDataRecord?format=json", files=files)
        d = r.json() if r.content else {}
        if d.get("statusCode") not in (None, 1):
            raise ISAPIError(f"Rostro rechazado por el equipo: {d.get('subStatusCode') or d.get('errorMsg')}")

    def face_url(self, employee_no):
        """URL de la foto de rostro registrada en el equipo para ese empleado (None si no tiene)."""
        d = self._json("POST", "/ISAPI/Intelligent/FDLib/FDSearch?format=json",
                       {"searchResultPosition": 0, "maxResults": 1, "faceLibType": "blackFD", "FDID": "1",
                        "FPID": str(employee_no)})
        return next((m.get("faceURL") for m in d.get("MatchList") or [] if m.get("faceURL")), None)

    def delete_face(self, employee_no):
        self._json("PUT", "/ISAPI/Intelligent/FDLib/FDSearch/Delete?format=json&FDID=1&faceLibType=blackFD",
                   {"FPID": [{"value": str(employee_no)}]})

    # ------------------------------------------------------------ eventos
    def search_events(self, start: str, end: str, max_total=5000):
        """Devuelve eventos de control de acceso entre start y end (ISO con zona horaria)."""
        events, pos, sid = [], 0, uuid.uuid4().hex[:16]
        while len(events) < max_total:
            cond = {"searchID": sid, "searchResultPosition": pos, "maxResults": 30,
                    "major": 5, "minor": 0, "startTime": start, "endTime": end}
            d = self._json("POST", "/ISAPI/AccessControl/AcsEvent?format=json", {"AcsEventCond": cond})
            s = d.get("AcsEvent", {})
            batch = s.get("InfoList", []) or []
            events.extend(batch)
            pos += len(batch)
            if s.get("responseStatusStrg") != "MORE" or not batch:
                break
        return events

    def stream(self, path):
        """Abre una conexión larga (alertStream). Usar como context manager."""
        client = httpx.Client(auth=self.auth, verify=self.verify,
                              timeout=httpx.Timeout(10.0, read=120.0))
        return _Stream(client, client.stream("GET", self.base + path))

    def fetch_picture(self, url: str) -> bytes:
        path = url.split("://", 1)[-1]
        path = "/" + path.split("/", 1)[1] if "/" in path else url
        return self._req("GET", path).content

    # ------------------------------------------------------------ puerta
    def door(self, cmd="open", door_no=1):
        """cmd: open | close | alwaysOpen | alwaysClose"""
        body = ('<RemoteControlDoor version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">'
                f"<cmd>{cmd}</cmd></RemoteControlDoor>")
        self._xml("PUT", f"/ISAPI/AccessControl/RemoteControl/door/{door_no}", body)


class _Stream:
    def __init__(self, client, cm):
        self.client, self.cm = client, cm

    def __enter__(self):
        r = self.cm.__enter__()
        if r.status_code >= 400:
            self.__exit__(None, None, None)
            raise ISAPIError(f"alertStream respondió {r.status_code}")
        return r

    def __exit__(self, *a):
        try:
            self.cm.__exit__(*a)
        finally:
            self.client.close()


def _strip_ns(el):
    for e in el.iter():
        if "}" in e.tag:
            e.tag = e.tag.split("}", 1)[1]
    return el


def _error_text(r):
    try:
        d = r.json()
        return d.get("errorMsg") or d.get("subStatusCode") or r.text[:200]
    except Exception:
        try:
            root = _strip_ns(ET.fromstring(r.content))
            sub = root.find("subStatusCode")
            return (sub.text if sub is not None else r.text[:200])
        except Exception:
            return r.text[:200]
