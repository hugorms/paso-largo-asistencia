import hashlib
import io
import os
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("ASISTENCIA_DATA", BASE_DIR / "datos"))
PHOTOS_DIR = DATA_DIR / "fotos"
DB_PATH = DATA_DIR / "asistencia.db"

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, full_name TEXT,
    password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'consulta',
    active INTEGER NOT NULL DEFAULT 1, created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS devices (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, host TEXT NOT NULL, port INTEGER DEFAULT 80,
    https INTEGER DEFAULT 0, username TEXT NOT NULL, password TEXT NOT NULL,
    tz_offset TEXT DEFAULT '-05:00', door_no INTEGER DEFAULT 1,
    direction TEXT DEFAULT 'ambos',          -- ambos | entrada | salida
    active INTEGER DEFAULT 1, model TEXT, serial TEXT, firmware TEXT, mac TEXT,
    online INTEGER DEFAULT 0, last_seen TEXT, last_error TEXT, last_event_time TEXT
);
CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, start_time TEXT NOT NULL, end_time TEXT NOT NULL,
    tolerance_min INTEGER DEFAULT 10, lunch_min INTEGER DEFAULT 0, days TEXT DEFAULT '1,2,3,4,5'
);
CREATE TABLE IF NOT EXISTS departments (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS employees (
    id INTEGER PRIMARY KEY, employee_no TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
    document TEXT, department_id INTEGER REFERENCES departments(id), position TEXT,
    email TEXT, phone TEXT, gender TEXT DEFAULT 'unknown',
    schedule_id INTEGER REFERENCES schedules(id), hire_date TEXT,
    active INTEGER DEFAULT 1, photo TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS employee_sync (
    employee_id INTEGER, device_id INTEGER, status TEXT, message TEXT, updated_at TEXT,
    PRIMARY KEY (employee_id, device_id)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, device_id INTEGER, serial_no INTEGER, event_time TEXT NOT NULL,
    employee_no TEXT, name TEXT, major INTEGER, minor INTEGER, verify_mode TEXT,
    attendance_status TEXT, picture_url TEXT, valid INTEGER DEFAULT 0, source TEXT DEFAULT 'equipo',
    UNIQUE (device_id, serial_no)
);
CREATE INDEX IF NOT EXISTS ix_events_emp_time ON events(employee_no, event_time);
CREATE INDEX IF NOT EXISTS ix_events_time ON events(event_time);
CREATE TABLE IF NOT EXISTS holidays (id INTEGER PRIMARY KEY, date TEXT UNIQUE NOT NULL, name TEXT);
CREATE TABLE IF NOT EXISTS leaves (
    id INTEGER PRIMARY KEY, employee_id INTEGER NOT NULL, date_from TEXT NOT NULL, date_to TEXT NOT NULL,
    kind TEXT NOT NULL, notes TEXT, created_by TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY, ts TEXT DEFAULT (datetime('now','localtime')), username TEXT,
    action TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
"""


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def tx():
    conn = connect()
    try:
        with _lock:
            yield conn
            conn.commit()
    finally:
        conn.close()


def q(sql, args=()):
    with tx() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def q1(sql, args=()):
    rows = q(sql, args)
    return rows[0] if rows else None


def ex(sql, args=()):
    with tx() as c:
        cur = c.execute(sql, args)
        return cur.lastrowid


def hash_password(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000).hex()
    return f"{salt}${h}"


def check_password(pw, stored):
    salt, _ = stored.split("$", 1)
    return secrets.compare_digest(hash_password(pw, salt), stored)


def get_setting(key, default=None):
    r = q1("SELECT value FROM settings WHERE key=?", (key,))
    return r["value"] if r else default


def int_setting(key, default):
    """Ajuste numérico; si quedó vacío o con un valor raro se usa el valor por defecto."""
    try:
        return int(get_setting(key, default))
    except (TypeError, ValueError):
        return default


def set_setting(key, value):
    ex("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
       (key, str(value)))


def audit(username, action, detail=""):
    ex("INSERT INTO audit(username, action, detail) VALUES (?,?,?)", (username, action, detail))


def _shift(hhmm, minutes):
    h, m = map(int, hhmm.split(":")[:2])
    t = min(max(h * 60 + m + minutes, 0), 23 * 60 + 59)
    return f"{t // 60:02d}:{t % 60:02d}"


def default_windows(start, end):
    """Entrada: 1 h 30 antes y después de la hora de inicio. Salida: desde la hora de salida hasta 3 h después."""
    return _shift(start, -90), _shift(start, 90), _shift(end, 0), _shift(end, 180)


def merge_device(c, keep, dup):
    """Une el registro dup en keep (mismo equipo físico) con todo su historial. Si dup se conectó más
    recientemente, keep se queda con su dirección y clave."""
    d = c.execute("SELECT * FROM devices WHERE id=?", (dup,)).fetchone()
    k = c.execute("SELECT * FROM devices WHERE id=?", (keep,)).fetchone()
    if not d or not k:
        return
    c.execute("UPDATE OR IGNORE events SET device_id=? WHERE device_id=?", (keep, dup))
    c.execute("DELETE FROM events WHERE device_id=?", (dup,))
    c.execute("UPDATE OR IGNORE employee_sync SET device_id=? WHERE device_id=?", (keep, dup))
    c.execute("DELETE FROM employee_sync WHERE device_id=?", (dup,))
    c.execute("""UPDATE devices SET active=MAX(active, ?), serial=COALESCE(serial, ?), mac=COALESCE(mac, ?),
                 model=COALESCE(model, ?), firmware=COALESCE(firmware, ?),
                 last_event_time=NULLIF(MAX(COALESCE(last_event_time,''), ?),'') WHERE id=?""",
              (d["active"], d["serial"], d["mac"], d["model"], d["firmware"], d["last_event_time"] or "", keep))
    c.execute("DELETE FROM devices WHERE id=?", (dup,))
    if (d["last_seen"] or "") > (k["last_seen"] or ""):
        c.execute("""UPDATE devices SET host=?, port=?, https=?, username=?, password=?, online=?, last_seen=?,
                     last_error=? WHERE id=?""", (d["host"], d["port"], d["https"], d["username"], d["password"],
                                                 d["online"], d["last_seen"], d["last_error"], keep))


def merge_same_serial(c):
    """El mismo equipo (mismo número de serie) registrado con dos IP distintas se une en el más antiguo."""
    dups = c.execute("""SELECT k.keep, d.id FROM devices d JOIN
                        (SELECT serial, MIN(id) AS keep FROM devices WHERE COALESCE(serial,'')<>''
                         GROUP BY serial HAVING COUNT(*)>1) k ON k.serial=d.serial WHERE d.id<>k.keep""").fetchall()
    for keep, dup in dups:
        merge_device(c, keep, dup)
    return len(dups)


def _merge_duplicate_devices(c):
    """Un mismo equipo (IP + puerto, o mismo número de serie) registrado varias veces se une en el primero."""
    c.execute("UPDATE devices SET host=lower(trim(host)) WHERE host<>lower(trim(host))")
    dups = c.execute("""SELECT k.keep, d.id FROM devices d JOIN
                        (SELECT host, port, MIN(id) AS keep FROM devices GROUP BY host, port HAVING COUNT(*)>1) k
                        ON k.host=d.host AND k.port=d.port WHERE d.id<>k.keep""").fetchall()
    for keep, dup in dups:
        merge_device(c, keep, dup)
    merge_same_serial(c)


def init():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
    with tx() as c:
        c.executescript(SCHEMA)
        cols = {r[1] for r in c.execute("PRAGMA table_info(employees)")}
        if "face_at" not in cols:
            # Fecha en que se registró el rostro: desde ahí cuenta la asistencia
            c.execute("ALTER TABLE employees ADD COLUMN face_at TEXT")
            c.execute("UPDATE employees SET face_at=substr(created_at,1,10) WHERE photo IS NOT NULL")
        scols = {r[1] for r in c.execute("PRAGMA table_info(schedules)")}
        if "in_from" not in scols:
            # Ventanas: solo cuentan como entrada/salida las marcas dentro de estos rangos
            for col in ("in_from", "in_to", "out_from", "out_to"):
                c.execute(f"ALTER TABLE schedules ADD COLUMN {col} TEXT")
            for sid, st, en in c.execute("SELECT id, start_time, end_time FROM schedules").fetchall():
                c.execute("UPDATE schedules SET in_from=?, in_to=?, out_from=?, out_to=? WHERE id=?",
                          default_windows(st, en) + (sid,))
        _merge_duplicate_devices(c)
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_devices_host_port ON devices(host, port)")
        if not c.execute("SELECT 1 FROM users").fetchone():
            c.execute("INSERT INTO users(username, full_name, password_hash, role) VALUES (?,?,?,?)",
                      ("admin", "Administrador", hash_password(os.environ.get("ADMIN_PASSWORD") or "admin123"), "admin"))
        if not c.execute("SELECT 1 FROM schedules").fetchone():
            c.execute("INSERT INTO schedules(name,start_time,end_time,tolerance_min,lunch_min,days,"
                      "in_from,in_to,out_from,out_to) VALUES ('Oficina L-V','08:00','17:00',10,60,'1,2,3,4,5',"
                      "'06:30','09:30','17:00','20:00')")
        c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES ('company','Paso Largo')")
        c.execute("UPDATE settings SET value='Paso Largo' WHERE key='company' AND value='Mi Empresa'")
        c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES ('poll_seconds','5')")
        c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES ('min_gap_minutes','2')")
    key_file = DATA_DIR / "secret.key"
    if not key_file.exists():
        key_file.write_text(secrets.token_hex(32))
    return key_file.read_text().strip()


BACKUP_DB = "asistencia.db"


def backup_zip() -> bytes:
    """Respaldo completo (base de datos + fotos) en un .zip, sin detener el programa."""
    import tempfile
    import zipfile
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / BACKUP_DB
        src, dst = connect(), sqlite3.connect(copy)
        try:
            src.backup(dst)  # copia consistente aunque haya escrituras en curso (WAL)
        finally:
            dst.close()
            src.close()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(copy, BACKUP_DB)
            for f in PHOTOS_DIR.glob("*"):
                if f.is_file():
                    z.write(f, f"fotos/{f.name}")
        return buf.getvalue()


def restore_zip(raw: bytes):
    """Reemplaza la base de datos y las fotos por las de un respaldo. Devuelve (empleados, eventos)."""
    import tempfile
    import zipfile
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise ValueError("El archivo no es un .zip válido.")
    if BACKUP_DB not in z.namelist():
        raise ValueError("El .zip no contiene asistencia.db (¿es un respaldo de Paso Largo?).")
    with tempfile.TemporaryDirectory() as tmp:
        new = Path(tmp) / BACKUP_DB
        new.write_bytes(z.read(BACKUP_DB))
        src = sqlite3.connect(new)
        try:
            tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"employees", "events", "users"} <= tables:
                raise ValueError("La base de datos del respaldo no es de Paso Largo.")
            counts = (src.execute("SELECT COUNT(*) FROM employees").fetchone()[0],
                      src.execute("SELECT COUNT(*) FROM events").fetchone()[0])
            with _lock:
                dst = connect()
                try:
                    src.backup(dst)
                finally:
                    dst.close()
        except sqlite3.DatabaseError as e:
            raise ValueError(f"La base de datos del respaldo está dañada: {e}")
        finally:
            src.close()
    for name in z.namelist():
        if name.startswith("fotos/") and not name.endswith("/"):
            (PHOTOS_DIR / Path(name).name).write_bytes(z.read(name))
    init()  # aplica columnas/tablas nuevas si el respaldo es de una versión anterior
    return counts
