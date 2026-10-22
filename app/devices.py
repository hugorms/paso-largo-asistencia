"""Operaciones sobre los equipos: estado, descarga de eventos y sincronización de empleados."""
import io
import logging
import threading
import time
from datetime import datetime, timedelta

from PIL import Image, ImageOps

from . import db
from .isapi import MINOR_FAIL, ISAPIClient, ISAPIError

log = logging.getLogger("asistencia")


def client(dev) -> ISAPIClient:
    return ISAPIClient(dev["host"], dev["username"], dev["password"], dev["port"] or 80, bool(dev["https"]))


def local_offset():
    """Zona horaria de esta PC, p. ej. '-04:00'. El equipo y la PC están en el mismo lugar."""
    z = datetime.now().astimezone().strftime("%z")
    return f"{z[:3]}:{z[3:]}"


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def refresh_info(dev):
    try:
        info = client(dev).device_info()
        db.ex("""UPDATE devices SET online=1, last_seen=?, last_error=NULL, model=?, serial=?, firmware=?, mac=?
                 WHERE id=?""", (_now(), info.get("model"), info.get("serialNumber"),
                                 info.get("firmwareVersion"), info.get("macAddress"), dev["id"]))
        return info
    except ISAPIError as e:
        db.ex("UPDATE devices SET online=0, last_error=? WHERE id=?", (str(e), dev["id"]))
        raise


def _local(ts: str) -> str:
    """Convierte la hora del equipo ('2026-10-22T11:26:08+08:00') a la hora local de esta PC."""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return ts[:19]
    if dt.tzinfo:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def posix_tz(offset: str) -> str:
    """'-04:00' -> 'CST+4:00:00' (formato POSIX que usa el equipo: el signo va invertido)."""
    sign = "+" if offset.startswith("-") else "-"
    h, m = offset[1:].split(":")
    return f"CST{sign}{int(h)}:{m}:00"


def pull_events(dev, since: datetime | None = None, until: datetime | None = None):
    """Descarga eventos del equipo y los guarda. Devuelve cuántos eventos nuevos hubo."""
    tz = local_offset()
    if since is None:
        last = dev["last_event_time"]
        if last and last > (datetime.now() + timedelta(minutes=10)).isoformat():
            last = None  # quedó guardada una hora "futura" de cuando el reloj del equipo estaba mal
        # Se mira al menos 2 h hacia atrás: si el reloj del equipo estaba adelantado y luego se corrige,
        # los eventos nuevos quedan "antes" del último guardado. Los duplicados se descartan por serialNo.
        since = (min(datetime.fromisoformat(last) - timedelta(minutes=5), datetime.now() - timedelta(hours=2))
                 if last else datetime.now() - timedelta(days=31))
    until = until or datetime.now() + timedelta(days=1)
    evs = client(dev).search_events(since.strftime("%Y-%m-%dT%H:%M:%S") + tz,
                                    until.strftime("%Y-%m-%dT%H:%M:%S") + tz)
    new, newest = 0, None
    cap = (datetime.now() + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S")  # ignora relojes adelantados
    with db.tx() as c:
        for e in evs:
            emp = str(e.get("employeeNoString") or e.get("employeeNo") or "").strip() or None
            t = _local(e.get("time", ""))
            if not t:
                continue
            minor = int(e.get("minor", 0))
            valid = 1 if emp and minor not in MINOR_FAIL else 0
            exists = c.execute("SELECT 1 FROM events WHERE device_id=? AND serial_no=?",
                               (dev["id"], e.get("serialNo"))).fetchone()
            # Si ya existía se actualiza la hora (corrige eventos guardados con zona horaria equivocada)
            c.execute("""INSERT INTO events(device_id, serial_no, event_time, employee_no, name,
                         major, minor, verify_mode, attendance_status, picture_url, valid)
                         VALUES (?,?,?,?,?,?,?,?,?,?,?)
                         ON CONFLICT(device_id, serial_no) DO UPDATE SET event_time=excluded.event_time""",
                      (dev["id"], e.get("serialNo"), t, emp, e.get("name"), e.get("major"), minor,
                       e.get("currentVerifyMode"), e.get("attendanceStatus"), e.get("pictureURL"), valid))
            new += 0 if exists else 1
            if (not newest or t > newest) and t <= cap:
                newest = t
        c.execute("""UPDATE devices SET last_event_time=COALESCE(?, CASE WHEN last_event_time > ? THEN NULL
                     ELSE last_event_time END), online=1, last_seen=?, last_error=NULL WHERE id=?""",
                  (newest, cap, _now(), dev["id"]))
    if evs:
        import_unknown_users(dev)
    return new


def import_unknown_users(dev):
    """Crea en el sistema a las personas que marcan en el equipo pero aún no están registradas aquí."""
    missing = [r["employee_no"] for r in db.q(
        """SELECT DISTINCT employee_no FROM events WHERE device_id=? AND valid=1 AND employee_no IS NOT NULL
           AND employee_no NOT IN (SELECT employee_no FROM employees)""", (dev["id"],))]
    if not missing:
        return 0
    sched = db.q1("SELECT id FROM schedules ORDER BY id")
    users = {u["employeeNo"]: u for u in client(dev).search_users()}
    for no in missing:
        u = users.get(no, {})
        db.ex("INSERT OR IGNORE INTO employees(employee_no, name, gender, schedule_id) VALUES (?,?,?,?)",
              (no, u.get("name") or f"Empleado {no}", u.get("gender", "unknown"), sched["id"] if sched else None))
    db.audit("sistema", "empleados importados del equipo", f"{dev['name']}: {', '.join(missing)}")
    return len(missing)


def check_clock(dev, max_drift=120):
    """Si el reloj del equipo difiere más de max_drift segundos de esta PC, lo corrige."""
    c = client(dev)
    info = c.get_time()
    t = info.get("localTime", "")
    if not t:
        return None
    drift = (datetime.fromisoformat(_local(t)) - datetime.now()).total_seconds()
    wrong_tz = info.get("timeZone") and info["timeZone"] != posix_tz(local_offset())
    if abs(drift) > max_drift or wrong_tz:
        c.set_time(datetime.now(), local_offset())
        db.audit("sistema", "hora del equipo corregida",
                 f"{dev['name']}: desfase {int(drift)} s, zona {info.get('timeZone')} -> {posix_tz(local_offset())}")
        log.info("%s: hora corregida (desfase %d s)", dev["name"], drift)
    return drift


def prepare_face(raw: bytes) -> bytes:
    """Normaliza la foto: orientación EXIF, RGB, máx. 800px y < 200 KB (límite típico del equipo)."""
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
    img.thumbnail((800, 800))
    for quality in (90, 80, 70, 60, 50, 40):
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality)
        if buf.tell() < 200 * 1024:
            break
    return buf.getvalue()


def push_employee(emp, devices=None):
    """Envía usuario + rostro a todos los equipos activos. Devuelve lista de (equipo, ok, mensaje)."""
    devices = devices or db.q("SELECT * FROM devices WHERE active=1")
    results = []
    for dev in devices:
        try:
            c = client(dev)
            c.upsert_user(emp["employee_no"], emp["name"], gender=emp.get("gender") or "unknown",
                          door_no=dev["door_no"] or 1)
            msg = "Usuario sincronizado"
            if emp.get("photo"):
                c.upload_face(emp["employee_no"], (db.PHOTOS_DIR / emp["photo"]).read_bytes())
                msg = "Usuario y rostro sincronizados"
            ok = True
        except ISAPIError as e:
            ok, msg = False, str(e)
        db.ex("""INSERT INTO employee_sync(employee_id, device_id, status, message, updated_at) VALUES (?,?,?,?,?)
                 ON CONFLICT(employee_id, device_id) DO UPDATE SET status=excluded.status,
                 message=excluded.message, updated_at=excluded.updated_at""",
              (emp["id"], dev["id"], "ok" if ok else "error", msg, _now()))
        results.append((dev["name"], ok, msg))
    return results


def remove_employee(emp):
    results = []
    for dev in db.q("SELECT * FROM devices WHERE active=1"):
        try:
            c = client(dev)
            try:
                c.delete_face(emp["employee_no"])
            except ISAPIError:
                pass
            c.delete_user(emp["employee_no"])
            results.append((dev["name"], True, "Eliminado"))
        except ISAPIError as e:
            results.append((dev["name"], False, str(e)))
    return results


class Poller(threading.Thread):
    """Hilo que descarga eventos de cada equipo. Se despierta al instante cuando un AlertListener
    recibe un aviso del equipo; si no, revisa cada N segundos como respaldo."""

    def __init__(self):
        super().__init__(daemon=True, name="poller")
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.listeners = {}
        self.clock_checked = {}

    def _ensure_listeners(self, devs):
        for dev in devs:
            key = (dev["id"], dev["host"], dev["port"], dev["username"], dev["password"])
            cur = self.listeners.get(dev["id"])
            if cur and cur.key == key and cur.is_alive():
                continue
            if cur:
                cur.stop.set()
            self.listeners[dev["id"]] = AlertListener(dev, key, self.wake)
            self.listeners[dev["id"]].start()
        active = {d["id"] for d in devs}
        for did in list(self.listeners):
            if did not in active:
                self.listeners.pop(did).stop.set()

    def run(self):
        while not self.stop.is_set():
            devs = db.q("SELECT * FROM devices WHERE active=1")
            self._ensure_listeners(devs)
            for dev in devs:
                try:
                    if time.time() - self.clock_checked.get(dev["id"], 0) > 600:
                        self.clock_checked[dev["id"]] = time.time()
                        check_clock(dev)
                    n = pull_events(dev)
                    if n:
                        log.info("%s: %d eventos nuevos", dev["name"], n)
                except ISAPIError as e:
                    db.ex("UPDATE devices SET online=0, last_error=? WHERE id=?", (str(e), dev["id"]))
                except Exception as e:  # nunca dejar morir el hilo
                    log.exception("Error leyendo %s", dev["name"])
                    db.ex("UPDATE devices SET online=0, last_error=? WHERE id=?", (str(e), dev["id"]))
            self.wake.wait(max(2, int(db.get_setting("poll_seconds", "5"))))
            if self.wake.is_set():
                time.sleep(0.7)  # el equipo tarda un instante en guardar el evento
                self.wake.clear()


class AlertListener(threading.Thread):
    """Mantiene abierto /ISAPI/Event/notification/alertStream: el equipo envía cada evento al momento."""

    def __init__(self, dev, key, wake):
        super().__init__(daemon=True, name=f"alert-{dev['id']}")
        self.dev, self.key, self.wake = dev, key, wake
        self.stop = threading.Event()

    def run(self):
        c = client(self.dev)
        while not self.stop.is_set():
            try:
                with c.stream("/ISAPI/Event/notification/alertStream") as r:
                    log.info("%s: escuchando eventos en tiempo real", self.dev["name"])
                    for chunk in r.iter_bytes():
                        if self.stop.is_set():
                            return
                        low = chunk.lower()
                        # se ignoran los latidos ("heartBeat"/videoloss inactivo)
                        if b"accesscontrollerevent" in low or (b"eventtype" in low and b"heartbeat" not in low
                                                                and b"videoloss" not in low):
                            self.wake.set()
            except Exception as e:
                log.debug("alertStream %s: %s", self.dev["name"], e)
            self.stop.wait(10)
