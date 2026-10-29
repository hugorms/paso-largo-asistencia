"""Operaciones sobre los equipos: estado, descarga de eventos y sincronización de empleados."""
import io
import ipaddress
import json
import logging
import re
import threading
import time
import unicodedata
from datetime import datetime, timedelta

from PIL import Image, ImageOps

try:  # fotos HEIC del iPhone/Samsung
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass

from . import db, discovery
from .isapi import MINOR_FAIL, ISAPIClient, ISAPIConnError, ISAPIError

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
        mine = db.q1("SELECT local_id FROM devices WHERE id=?", (dev["id"],))
        if info.get("serialNumber") and mine and mine["local_id"] is not None and db.q1(
                "SELECT 1 FROM devices WHERE serial=? AND id<>? AND local_id IS NOT NULL AND local_id<>?",
                (info["serialNumber"], dev["id"], mine["local_id"])):
            # Es el mismo aparato que otro local ya tiene registrado (con otra IP o nombre): no se une ni se lee
            # (si no, sus marcas se copiarían a este local). Queda pausado hasta que el admin lo revise.
            msg = f"Ese biométrico {OTHER_LOCAL}; se dejó pausado. Pídele al administrador que lo revise."
            db.ex("UPDATE devices SET active=0, online=0, paused_other=1, last_error=? WHERE id=?", (msg, dev["id"]))
            raise ISAPIError(msg)
        with db.tx() as c:
            c.execute("""UPDATE devices SET online=1, last_seen=?, last_error=NULL, paused_other=0, model=?, serial=?, firmware=?, mac=?
                         WHERE id=?""", (_now(), info.get("model"), info.get("serialNumber"),
                                         info.get("firmwareVersion"), info.get("macAddress"), dev["id"]))
            # si este mismo equipo ya estaba registrado con otra IP, se une a ese registro
            db.merge_same_serial(c)
        return info
    except ISAPIError as e:
        db.ex("UPDATE devices SET online=0, last_error=? WHERE id=?", (str(e), dev["id"]))
        raise


# ------------------------------------------------------------------ cambio de red / de IP
def _try_at(dev, host, ports):
    """Prueba la clave guardada del equipo en host con varias combinaciones (puerto, https).
    Devuelve (puerto, https, info) del primero que responde."""
    err = None
    for port, https in dict.fromkeys(ports):
        try:
            return port, https, ISAPIClient(host, dev["username"], dev["password"], port, https,
                                            timeout=6).device_info()
        except ISAPIConnError as e:
            err = err or e
        except ISAPIError as e:  # responde, pero p. ej. clave incorrecta: ese mensaje es el útil
            err = e
            break
    raise err or ISAPIConnError("No se pudo conectar con el equipo")


def _is_same(dev, info):
    return not (dev["serial"] or dev["mac"]) or discovery.same_device(
        dev["serial"], dev["mac"], info.get("serialNumber"), info.get("macAddress"))


def move_device(dev, host, port, https):
    """Cambia la dirección del equipo ya registrado (no crea otro). Si esa IP estaba en otro registro
    del mismo equipo, se unen."""
    host = host.strip().lower()
    with db.tx() as c:
        other = c.execute("SELECT id, name, serial, mac, local_id FROM devices WHERE host=? AND port=? AND id<>?",
                          (host, port, dev["id"])).fetchone()
        if other:
            mine = c.execute("SELECT local_id FROM devices WHERE id=?", (dev["id"],)).fetchone()
            if other["local_id"] is not None and mine and mine["local_id"] is not None \
                    and other["local_id"] != mine["local_id"]:
                raise ISAPIError(f"La dirección {host}:{port} ya es de un equipo de otro local.")
            if (other["serial"] or other["mac"]) and not discovery.same_device(
                    dev["serial"], dev["mac"], other["serial"], other["mac"]):
                raise ISAPIError(f"La dirección {host}:{port} figura como otro equipo («{other['name']}»). "
                                 "Desactívalo o cámbiale la IP primero.")
            db.merge_device(c, dev["id"], other["id"])
        c.execute("UPDATE devices SET host=?, port=?, https=? WHERE id=?", (host, port, int(https), dev["id"]))
    if (host, port) != (dev["host"], dev["port"]):
        db.audit("sistema", "equipo cambió de IP", f"{dev['name']}: {dev['host']}:{dev['port']} -> {host}:{port}")
        log.info("%s: ahora en %s:%s", dev["name"], host, port)


def connect_at(dev, host, port=None):
    """Botón «Probar y conectar»: prueba la IP nueva y, si responde el mismo equipo, la guarda."""
    port = int(port or dev["port"] or 80)
    host = host.strip().lower()
    port, https, info = _try_at(dev, host, [(port, bool(dev["https"])), (port, not dev["https"])])
    if not _is_same(dev, info):
        raise ISAPIError(f"En {host} responde otro equipo ({info.get('model')}, serie {info.get('serialNumber')}), "
                         f"no «{dev['name']}».")
    move_device(dev, host, port, https)
    return refresh_info(db.q1("SELECT * FROM devices WHERE id=?", (dev["id"],)))


def _ports_for(dev, cand):
    return [(dev["port"] or 80, bool(dev["https"])), (cand["port"], cand["https"]), (443, True), (80, False)]


def relocate(dev, found=None):
    """El equipo no responde en su IP: lo busca en la red por número de serie (SADP) o probando su clave en
    los equipos ISAPI de la subred, y si lo encuentra en otra IP la actualiza. Devuelve (nueva_ip|None, nota)."""
    if not (dev["serial"] or dev["mac"]):
        return None, "No se puede buscar solo: el equipo nunca se conectó (no se conoce su número de serie)."
    if found is None:
        with discovery._lock:
            found = discovery.sadp()
        if not any(discovery.same_device(dev["serial"], dev["mac"], f["serial"], f["mac"]) for f in found):
            found = discovery.find(ports={80, 443, dev["port"] or 80})
    nets = ", ".join(str(n) for n in discovery.networks()) or "ninguna (¿la PC está conectada?)"
    seen_at = None
    for f in found:
        if f["serial"] or f["mac"]:
            if not discovery.same_device(dev["serial"], dev["mac"], f["serial"], f["mac"]):
                continue
            seen_at = f
        try:
            port, https, info = _try_at(dev, f["ip"], _ports_for(dev, f))
        except ISAPIError:
            continue
        if _is_same(dev, info):
            move_device(dev, f["ip"], port, https)
            refresh_info(db.q1("SELECT * FROM devices WHERE id=?", (dev["id"],)))
            return f"{f['ip']}:{port}", ""
    here = seen_at and any(ipaddress.ip_address(seen_at["ip"]) in n for n in discovery.networks())
    if seen_at and seen_at["ip"] == dev["host"]:
        return None, (f"El equipo aparece en la red con su misma IP ({seen_at['ip']}) pero no acepta la conexión: "
                      "puede estar reiniciándose.")
    if here:
        return None, (f"El equipo apareció en la IP {seen_at['ip']}, pero no acepta la conexión con el usuario y "
                      "la clave guardados. Prueba con «Probar y conectar» o revisa la clave en Editar.")
    if seen_at:
        return None, (f"El equipo apareció en la red con la IP {seen_at['ip']} (máscara {seen_at.get('mask') or '?'}), "
                      f"pero esta PC no llega a él: está en otra red ({nets}). Ponle al equipo una IP de esta red "
                      "(con la herramienta SADP o en el equipo: Comunicación → Red) o activa DHCP.")
    return None, f"Se buscó en la red ({nets}) y no apareció. Revisa que esté encendido y con el cable de red."


def search_network():
    """Botón «Buscar biométrico»: lista los equipos de la red, reconoce los ya registrados (por serie)
    y les corrige la IP si cambió. Devuelve (encontrados, redes)."""
    devs = db.q("SELECT * FROM devices")
    found = discovery.find(ports={80, 443} | {d["port"] for d in devs if d["port"]})
    for f in found:
        if not f["serial"]:  # del barrido: con la clave de algún equipo registrado se sabe cuál es
            for d in devs:
                try:
                    _, _, info = _try_at(d, f["ip"], [(f["port"], f["https"])])
                except ISAPIError:
                    continue
                f.update(serial=info.get("serialNumber") or "", mac=info.get("macAddress") or "",
                         model=info.get("model") or "")
                break
        dev = next((d for d in devs if (d["serial"] or d["mac"]) and discovery.same_device(
            d["serial"], d["mac"], f["serial"], f["mac"])), None)
        f["dev_id"], f["dev_name"], f["status"] = (dev or {}).get("id"), (dev or {}).get("name"), "nuevo"
        if not dev:
            continue
        if dev["host"] == f["ip"]:
            f["status"] = "registrado"
            continue
        moved, note = relocate(dev, [f])
        f["status"], f["note"] = ("movido", f"IP actualizada: {dev['host']} → {f['ip']}") if moved else ("sin_acceso", note)
    return found, [str(n) for n in discovery.networks()]


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


def _save_event(c, dev, e):
    """Guarda un evento del equipo (de la búsqueda o del aviso en vivo). Devuelve (es_nuevo, hora_local)."""
    emp = str(e.get("employeeNoString") or e.get("employeeNo") or "").strip() or None
    t = _local(e.get("time", ""))
    if not t:
        return False, None
    minor = int(e.get("minor", 0))
    valid = 1 if emp and minor not in MINOR_FAIL else 0
    exists = c.execute("SELECT 1 FROM events WHERE device_id=? AND serial_no=?",
                       (dev["id"], e.get("serialNo"))).fetchone()
    # Si ya existía se actualiza la hora (corrige eventos guardados con zona horaria equivocada)
    # y se completan los datos que el aviso en vivo no trae (p. ej. la foto)
    c.execute("""INSERT INTO events(device_id, serial_no, event_time, employee_no, name,
                 major, minor, verify_mode, attendance_status, picture_url, valid)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?)
                 ON CONFLICT(device_id, serial_no) DO UPDATE SET event_time=excluded.event_time,
                 name=COALESCE(excluded.name, name), verify_mode=COALESCE(excluded.verify_mode, verify_mode),
                 attendance_status=COALESCE(excluded.attendance_status, attendance_status),
                 picture_url=COALESCE(excluded.picture_url, picture_url)""",
              (dev["id"], e.get("serialNo"), t, emp, e.get("name") or None, e.get("major"), minor,
               e.get("currentVerifyMode"), e.get("attendanceStatus"), e.get("pictureURL"), valid))
    return not exists, t


def save_live_event(dev, e):
    """Guarda al instante el evento que el equipo acaba de avisar por alertStream."""
    with db.tx() as c:
        is_new, _ = _save_event(c, dev, e)
    if is_new and e.get("employeeNoString") and not db.q1(
            "SELECT 1 FROM employees WHERE employee_no=?", (str(e["employeeNoString"]),)):
        import_unknown_users(dev)
    return is_new


def pull_events(dev, since: datetime | None = None, until: datetime | None = None, quick=False, deep=False):
    """Descarga eventos del equipo y los guarda. Devuelve cuántos eventos nuevos hubo.
    quick: solo los últimos minutos (cuando el equipo acaba de avisar una marca).
    deep: repasa al menos las últimas 2 horas (se hace cada 10 min, no en cada vuelta)."""
    tz = local_offset()
    if since is None and quick and dev["last_event_time"]:
        since = min(datetime.fromisoformat(dev["last_event_time"]), datetime.now()) - timedelta(minutes=2)
    if since is None:
        last = dev["last_event_time"]
        if last and last > (datetime.now() + timedelta(minutes=10)).isoformat():
            last = None  # quedó guardada una hora "futura" de cuando el reloj del equipo estaba mal
        # En el repaso se mira al menos 2 h hacia atrás: si el reloj del equipo estaba adelantado y luego
        # se corrige, los eventos nuevos quedan "antes" del último guardado. Los duplicados se descartan por serialNo.
        if not last:
            since = datetime.now() - timedelta(days=31)
        elif deep:
            since = min(datetime.fromisoformat(last) - timedelta(minutes=5), datetime.now() - timedelta(hours=2))
        else:
            since = datetime.fromisoformat(last) - timedelta(minutes=5)
    until = until or datetime.now() + timedelta(days=1)
    evs = client(dev).search_events(since.strftime("%Y-%m-%dT%H:%M:%S") + tz,
                                    until.strftime("%Y-%m-%dT%H:%M:%S") + tz)
    new, newest = 0, None
    cap = (datetime.now() + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S")  # ignora relojes adelantados
    with db.tx() as c:
        for e in evs:
            is_new, t = _save_event(c, dev, e)
            if not t:
                continue
            new += is_new
            if (not newest or t > newest) and t <= cap:
                newest = t
        c.execute("""UPDATE devices SET last_event_time=COALESCE(?, CASE WHEN last_event_time > ? THEN NULL
                     ELSE last_event_time END), online=1, last_seen=?, last_error=NULL WHERE id=?""",
                  (newest, cap, _now(), dev["id"]))
    if evs:
        import_unknown_users(dev)
    return new


def local_schedule(local_id):
    """Horario que se pone a un empleado creado solo (desde un equipo): el primero de su local, o ninguno."""
    if local_id is None:
        return None
    r = db.q1("SELECT id FROM schedules WHERE local_id=? ORDER BY id", (local_id,))
    return r["id"] if r else None


def add_from_device(dev, no, name, gender="unknown", face_at=None):
    """Da de alta a una persona que está en el equipo: queda en el local del equipo, con un horario de ese local.
    Si el N° ya existe (en cualquier local) no hace nada: el N° es único en toda la empresa."""
    return db.ex("""INSERT OR IGNORE INTO employees(employee_no, name, gender, schedule_id, face_at, local_id)
                    VALUES (?,?,?,?,?,?)""",
                 (no, name or f"Empleado {no}", gender or "unknown", local_schedule(dev["local_id"]), face_at,
                  dev["local_id"]))


def import_unknown_users(dev):
    """Crea en el sistema a las personas que marcan en el equipo pero aún no están registradas aquí."""
    missing = [r["employee_no"] for r in db.q(
        """SELECT DISTINCT employee_no FROM events WHERE device_id=? AND valid=1 AND employee_no IS NOT NULL
           AND employee_no NOT IN (SELECT employee_no FROM employees)""", (dev["id"],))]
    if not missing:
        return 0
    users = {u["employeeNo"]: u for u in client(dev).search_users()}
    for no in missing:
        u = users.get(no, {})
        add_from_device(dev, no, u.get("name"), u.get("gender", "unknown"))
    db.audit("sistema", "empleados importados del equipo", f"{dev['name']}: {', '.join(missing)}")
    return len(missing)


def sync_faces(dev):
    """Da de alta a quien ya tiene rostro registrado en el equipo (aunque aún no marque) y anota
    desde cuándo tiene rostro. Quien no tiene rostro no cuenta asistencia."""
    users = [u for u in client(dev).search_users() if int(u.get("numOfFace") or 0) > 0]
    today = datetime.now().date().isoformat()
    new = []
    for u in users:
        no = str(u.get("employeeNo") or "").strip()
        if not no:
            continue
        if db.q1("SELECT 1 FROM employees WHERE employee_no=?", (no,)):
            db.ex("UPDATE employees SET face_at=? WHERE employee_no=? AND face_at IS NULL", (today, no))
        else:
            add_from_device(dev, no, u.get("name"), u.get("gender", "unknown"), today)
            new.append(no)
    if new:
        db.audit("sistema", "empleados con rostro importados del equipo", f"{dev['name']}: {', '.join(new)}")
    return len(new)


# Equipos cuya hora no se corrigió sola porque la del servidor parece incorrecta (id -> aviso)
CLOCK_WARNINGS = {}


def check_clock(dev, max_drift=120):
    """Si el reloj del equipo difiere más de max_drift segundos de esta PC, lo corrige.
    Si la hora del servidor parece mala (año viejo, o muy distinta a la de un equipo con fecha válida,
    p. ej. tras un corte de luz sin internet), no la copia: avisa para no marcar todo con hora errónea."""
    c = client(dev)
    info = c.get_time()
    t = info.get("localTime", "")
    if not t:
        return None
    dev_time, now = datetime.fromisoformat(_local(t)), datetime.now()
    drift = (dev_time - now).total_seconds()
    wrong_tz = info.get("timeZone") and info["timeZone"] != posix_tz(local_offset())
    if now.year < 2025 or (abs(drift) > 6 * 3600 and dev_time.year >= 2025):
        msg = (f"La hora del servidor ({now:%d/%m/%Y %H:%M}) y la del equipo ({dev_time:%d/%m/%Y %H:%M}) no coinciden. "
               "No se corrigió sola por seguridad: revisa la hora del servidor y usa «Sincronizar hora».")
        if CLOCK_WARNINGS.get(dev["id"]) != msg:
            log.warning("%s: %s", dev["name"], msg)
            db.audit("sistema", "hora del equipo NO corregida", f"{dev['name']}: desfase {int(drift)} s")
        CLOCK_WARNINGS[dev["id"]] = msg
        return 0
    CLOCK_WARNINGS.pop(dev["id"], None)
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


OTHER_LOCAL = "ya está registrado en otro local"

# Equipos que se leen: activos y de un local activo (o todavía sin local)
POLLED = """SELECT * FROM devices WHERE active=1
            AND (local_id IS NULL OR local_id IN (SELECT id FROM locales WHERE active=1))"""


def local_devices(local_id):
    """Equipos activos de un local (ninguno si el empleado aún no tiene local)."""
    if local_id is None:
        return []
    return db.q("SELECT * FROM devices WHERE active=1 AND local_id=? ORDER BY id", (local_id,))


def push_employee(emp, devices=None):
    """Envía usuario + rostro a los equipos activos DEL LOCAL del empleado (no a los de otros locales).
    Devuelve lista de (equipo, ok, mensaje)."""
    devices = devices if devices is not None else local_devices(emp["local_id"])
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


def remove_employee(emp, local_ids=None):
    """Borra el usuario y su rostro de los equipos activos.
    local_ids=None: de los equipos de su local y de cualquier otro equipo donde el sistema lo subió (employee_sync).
    No se toca un equipo de otro local donde nunca lo subimos: ese N° allí puede ser otra persona de ese local.
    local_ids=[...]: solo los equipos de esos locales (al pasar a la persona a otro local se retira del anterior)."""
    if local_ids is None:
        devs = db.q("""SELECT * FROM devices WHERE active=1 AND (local_id IS ? OR local_id=? OR id IN
                       (SELECT device_id FROM employee_sync WHERE employee_id=?)) ORDER BY id""",
                    (emp["local_id"], emp["local_id"], emp["id"]))
    else:
        devs = [d for lid in local_ids for d in local_devices(lid)]
    results = []
    for dev in devs:
        try:
            c = client(dev)
            try:
                c.delete_face(emp["employee_no"])
            except ISAPIError:
                pass
            c.delete_user(emp["employee_no"])
            results.append((dev["name"], True, "Eliminado"))
            # ya no está en ese equipo: no debe seguir figurando como «En equipo»
            db.ex("DELETE FROM employee_sync WHERE employee_id=? AND device_id=?", (emp["id"], dev["id"]))
        except ISAPIError as e:
            results.append((dev["name"], False, str(e)))
    return results


# ------------------------------------------------------------------ varios equipos
def _meaningful(name, no):
    """Nombre normalizado si es un nombre real (no vacío, ni el número, ni «Empleado N»)."""
    n = unicodedata.normalize("NFKD", (name or "").strip().lower())
    n = " ".join("".join(ch for ch in n if not unicodedata.combining(ch)).split())
    return "" if not n or n == str(no).lower() or n == f"empleado {str(no).lower()}" else n


def same_local_devices(device_ids):
    """Equipos activos de esa lista, todos de un mismo local (si se mezclan locales: ValueError)."""
    ids = [int(i) for i in device_ids]
    if not ids:
        return []
    devs = db.q(f"SELECT * FROM devices WHERE active=1 AND id IN ({','.join('?' * len(ids))}) ORDER BY id", ids)
    if len({d["local_id"] for d in devs}) > 1:
        raise ValueError("Solo se comparan equipos de un mismo local.")
    return devs


def compare_users(device_ids):
    """Compara los usuarios de los equipos activos indicados (todos de un mismo local). Con dos o más biométricos,
    una misma persona debe tener el MISMO N° de empleado en todos: si no, sus marcas se cuentan como de otra persona.
    Devuelve (equipos, filas, errores). Estado de cada fila: conflicto | duplicado | falta | ok.
    De cada fila solo se da el nombre del sistema (app_name/emp_id) si el empleado es de ese local; si el N° es de
    alguien de otro local, other_local=True y nada más."""
    devs, errors, data = same_local_devices(device_ids), {}, {}
    local_id = devs[0]["local_id"] if devs else None
    for dev in devs:
        try:
            data[dev["id"]] = {str(u.get("employeeNo") or "").strip(): u for u in client(dev).search_users()}
        except ISAPIError as e:
            errors[dev["id"]] = str(e)
    devs = [d for d in devs if d["id"] in data]
    app_names = {e["employee_no"]: e for e in db.q("SELECT id, employee_no, name, active, local_id FROM employees")}
    nos = sorted({no for users in data.values() for no in users if no},
                 key=lambda n: (not n.isdigit(), int(n) if n.isdigit() else 0, n))
    rows = []
    for no in nos:
        per = {}
        for dev in devs:
            u = data[dev["id"]].get(no)
            per[dev["id"]] = u and {"name": u.get("name") or "", "faces": int(u.get("numOfFace") or 0)}
        names = {_meaningful(v["name"], no) for v in per.values() if v} - {""}
        emp = app_names.get(no) or {}
        other = bool(emp) and emp["local_id"] != local_id
        if other:
            emp = {}
        st = ("conflicto" if len(names) > 1 else
              "falta" if any(not v or not v["faces"] for v in per.values()) else "ok")
        rows.append({"no": no, "app_name": emp.get("name"), "emp_id": emp.get("id"), "per": per, "status": st,
                     "names": names, "other_local": other})
    # La misma persona (mismo nombre) con N° distinto en cada equipo
    by_name = {}
    for r in rows:
        for n in r["names"]:
            by_name.setdefault(n, set()).add(r["no"])
    for r in rows:
        others = sorted(set().union(*(by_name[n] for n in r["names"])) - {r["no"]}) if r["names"] else []
        if others and r["status"] != "conflicto":
            r["status"], r["others"] = "duplicado", others
    return devs, rows, errors


def copy_to_missing(nos, device_ids):
    """Copia usuario + rostro desde el equipo que lo tiene a los equipos indicados (activos, de un mismo local)
    donde falta (o no tiene rostro). Nunca toca un N° que en otro equipo es de otra persona.
    Devuelve (copiados, mensajes)."""
    devs, rows, errors = compare_users(device_ids)
    by_id = {d["id"]: d for d in devs}
    done, msgs = 0, []
    for r in rows:
        if r["no"] not in nos:
            continue
        if r["status"] != "falta":
            msgs.append(f"N° {r['no']}: no se copió ({r['status']}), revísalo a mano.")
            continue
        src = next((by_id[did] for did, v in r["per"].items() if v and v["faces"]), None)
        if not src:
            msgs.append(f"N° {r['no']}: no tiene rostro en ningún equipo.")
            continue
        try:
            sc = client(src)
            url = sc.face_url(r["no"])
            jpeg = prepare_face(sc.fetch_picture(url)) if url else None
        except Exception as e:
            msgs.append(f"N° {r['no']}: no se pudo leer el rostro de «{src['name']}»: {e}")
            continue
        if not jpeg:
            msgs.append(f"N° {r['no']}: «{src['name']}» no entregó la foto del rostro.")
            continue
        name = r["app_name"] or r["per"][src["id"]]["name"] or f"Empleado {r['no']}"
        for did, v in r["per"].items():
            if v and v["faces"]:
                continue
            dev = by_id[did]
            try:
                c = client(dev)
                if not v:
                    c.upsert_user(r["no"], name, door_no=dev["door_no"] or 1)
                c.upload_face(r["no"], jpeg)
                ok, m = True, f"Copiado desde {src['name']}"
                done += 1
            except ISAPIError as e:
                ok, m = False, str(e)
                msgs.append(f"N° {r['no']} en «{dev['name']}»: {e}")
            if r["emp_id"]:
                db.ex("""INSERT INTO employee_sync(employee_id, device_id, status, message, updated_at) VALUES (?,?,?,?,?)
                         ON CONFLICT(employee_id, device_id) DO UPDATE SET status=excluded.status,
                         message=excluded.message, updated_at=excluded.updated_at""",
                      (r["emp_id"], did, "ok" if ok else "error", m, _now()))
    if done:
        db.audit("sistema", "rostros copiados entre equipos", ", ".join(nos))
    return done, msgs


class Poller(threading.Thread):
    """Hilo que descarga eventos de cada equipo. Se despierta al instante cuando un AlertListener
    recibe un aviso del equipo; si no, revisa cada N segundos como respaldo."""

    def __init__(self):
        super().__init__(daemon=True, name="poller")
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.listeners = {}
        self.clock_checked = {}
        self.faces_checked = {}
        self.deep_checked = {}
        self.fails = {}     # vueltas seguidas sin respuesta, por equipo
        self.down_since = {}  # desde cuándo no responde
        self.searched = {}  # última vez que se lo buscó en la red
        self.notes = {}     # resultado de esa búsqueda, se muestra junto al error
        self.searching = set()  # equipos que se están buscando ahora mismo

    def _ensure_listeners(self, devs):
        for dev in devs:
            key = (dev["id"], dev["host"], dev["port"], dev["https"], dev["username"], dev["password"])
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

    def _quick_pull(self):
        """El equipo avisó una marca: solo se descargan los últimos minutos, sin tareas de mantenimiento."""
        for dev in db.q(POLLED):
            if self.fails.get(dev["id"]):
                continue  # un equipo caído no debe demorar las marcas del otro; lo revisa la vuelta normal
            try:
                n = pull_events(dev, quick=True)
                if n:
                    log.info("%s: %d eventos nuevos (en vivo)", dev["name"], n)
            except ISAPIError as e:
                log.debug("descarga rápida %s: %s", dev["name"], e)

    def _relocate(self, dev, err):
        """Si el equipo dejó de responder (p. ej. la PC o el equipo cambiaron de red), se lo busca en la red
        y se le actualiza la IP. Se intenta a la 2.ª vuelta sin respuesta, luego cada 30 s durante los primeros
        10 minutos (tras un corte de luz el equipo tarda en arrancar y tomar el Wi-Fi) y después cada 2 minutos."""
        did = dev["id"]
        self.fails[did] = self.fails.get(did, 0) + 1
        self.down_since.setdefault(did, time.time())
        every = 30 if time.time() - self.down_since[did] < 600 else 120
        if self.fails[did] < 2 or time.time() - self.searched.get(did, 0) < every:
            return
        if did in self.searching:
            return
        self.searched[did] = time.time()
        # La búsqueda tarda varios segundos: se hace aparte para no demorar las marcas de los otros equipos
        self.searching.add(did)
        threading.Thread(target=self._search, args=(dev, err), daemon=True, name=f"buscar-{did}").start()

    def _search(self, dev, err):
        did = dev["id"]
        log.info("%s: no responde en %s, buscándolo en la red…", dev["name"], dev["host"])
        try:
            moved, note = relocate(dev)
        except Exception as e:
            log.exception("Error buscando %s en la red", dev["name"])
            moved, note = None, f"No se pudo buscar en la red: {e}"
        finally:
            self.searching.discard(did)
        if moved:
            self._reconnected(did)
        else:
            self.notes[did] = note
            db.ex("UPDATE devices SET last_error=? WHERE id=?", (f"{err}. {note}", did))

    def _reconnected(self, did):
        """El equipo vuelve a responder (en su IP o en una nueva): se revisa su hora y se descarga
        todo lo que marcaron mientras estaba desconectado, sin esperar a la revisión de cada 10 minutos."""
        self.fails.pop(did, None)
        self.down_since.pop(did, None)
        self.notes.pop(did, None)
        self.clock_checked.pop(did, None)
        self.deep_checked.pop(did, None)
        self.wake.set()

    def run(self):
        while not self.stop.is_set():
            try:
                self._loop()
            except Exception:  # nunca dejar morir el hilo
                log.exception("Error en el lector de marcas")
                self.stop.wait(5)

    def _loop(self):
        last_full = 0
        while not self.stop.is_set():
            if self.wake.is_set():
                time.sleep(0.4)  # el equipo tarda un instante en guardar el evento
                self.wake.clear()
                self._quick_pull()
                if time.time() - last_full < max(2, db.int_setting("poll_seconds", 5)):
                    self.wake.wait(1)
                    continue
            last_full = time.time()
            devs = db.q(POLLED)
            self._ensure_listeners(devs)
            for dev in devs:
                try:
                    if time.time() - self.clock_checked.get(dev["id"], 0) > 600:
                        self.clock_checked[dev["id"]] = time.time()
                        refresh_info(dev)  # si resulta ser el aparato de otro local, queda pausado y no se lee
                        if abs(check_clock(dev) or 0) > 120:
                            self.deep_checked.pop(dev["id"], None)  # tras corregir la hora, repaso completo
                    if time.time() - self.faces_checked.get(dev["id"], 0) > 60:
                        self.faces_checked[dev["id"]] = time.time()
                        if sync_faces(dev):
                            log.info("%s: empleados con rostro nuevos importados", dev["name"])
                    deep = time.time() - self.deep_checked.get(dev["id"], 0) > 600
                    if deep:
                        self.deep_checked[dev["id"]] = time.time()
                    n = pull_events(dev, deep=deep)
                    if n:
                        log.info("%s: %d eventos nuevos", dev["name"], n)
                    if self.fails.get(dev["id"]):
                        log.info("%s: volvió a responder en %s", dev["name"], dev["host"])
                        self._reconnected(dev["id"])
                except ISAPIError as e:
                    db.ex("UPDATE devices SET online=0, last_error=? WHERE id=?",
                          (". ".join(filter(None, [str(e), self.notes.get(dev["id"])])), dev["id"]))
                    if isinstance(e, ISAPIConnError):
                        self._relocate(dev, e)
                except Exception as e:  # nunca dejar morir el hilo
                    log.exception("Error leyendo %s", dev["name"])
                    db.ex("UPDATE devices SET online=0, last_error=? WHERE id=?", (str(e), dev["id"]))
            self.wake.wait(max(2, db.int_setting("poll_seconds", 5)))


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
                    buf = b""
                    for chunk in r.iter_bytes():
                        if self.stop.is_set():
                            return
                        low = chunk.lower()
                        # se ignoran los latidos ("heartBeat"/videoloss inactivo)
                        if b"accesscontrollerevent" in low or (b"eventtype" in low and b"heartbeat" not in low
                                                                and b"videoloss" not in low):
                            self.wake.set()
                        buf = self._parse(buf + chunk)
            except Exception as e:
                log.debug("alertStream %s: %s", self.dev["name"], e)
            self.stop.wait(10)

    def _parse(self, buf):
        """Separa las partes del flujo multipart; cada aviso JSON de marcación se guarda en el momento.
        Devuelve lo que queda pendiente en el búfer."""
        while True:
            i = buf.find(b"\r\n\r\n")
            if i < 0:
                return buf[-65536:]
            head = buf[:i].lower()
            m = re.search(rb"content-length:\s*(\d+)", head)
            if not m:
                buf = buf[i + 4:]
                continue
            end = i + 4 + int(m.group(1))
            if len(buf) < end:
                return buf if len(buf) < 4_000_000 else b""
            body, buf = buf[i + 4:end], buf[end:]
            if b"json" in head and b"accesscontrollerevent" in body.lower():
                try:
                    self._live(json.loads(body))
                except Exception as e:
                    log.debug("aviso en vivo %s: %s", self.dev["name"], e)

    def _live(self, d):
        a = d.get("AccessControllerEvent") or {}
        if a.get("serialNo") is None or int(a.get("majorEventType") or 5) != 5:
            return  # sin número de serie no se puede evitar duplicados: lo trae la descarga rápida
        e = {"time": d.get("dateTime"), "serialNo": a["serialNo"], "major": 5, "minor": a.get("subEventType", 0),
             "employeeNoString": a.get("employeeNoString") or a.get("employeeNo"), "name": a.get("name"),
             "currentVerifyMode": a.get("currentVerifyMode"), "attendanceStatus": a.get("attendanceStatus"),
             "pictureURL": a.get("pictureURL") or d.get("pictureURL")}
        if save_live_event(self.dev, e):
            log.info("%s: marca en vivo de %s", self.dev["name"], e["employeeNoString"] or "—")
