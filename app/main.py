import asyncio
import base64
import io
import logging
import re
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from openpyxl import load_workbook
from starlette.middleware.sessions import SessionMiddleware

from . import attendance, db, devices, reports, seguridad
from .isapi import MINOR_FAIL, MINOR_NAMES, ISAPIError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
SECRET = db.init()

app = FastAPI(title="Paso Largo", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
app.add_middleware(SessionMiddleware, secret_key=SECRET, max_age=12 * 3600, same_site="lax")
# Va después de la sesión para quedar por fuera: CSRF, cabeceras de seguridad y cookie segura por HTTPS
app.middleware("http")(seguridad.middleware)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.globals.update(local_offset=devices.local_offset, hm=attendance.hm, colors=attendance.STATUS_COLORS, minor_names=MINOR_NAMES,
                             days=reports.DAYS, today=date.today, clock_warnings=devices.CLOCK_WARNINGS,
                             server_now=lambda: datetime.now().strftime("%d/%m/%Y %H:%M"))

# En el servidor la imagen trae /app/VERSION con la fecha del despliegue; en la PC no existe
_version_file = Path(__file__).parent.parent / "VERSION"
templates.env.globals["version"] = _version_file.read_text().strip() if _version_file.exists() else "local"

DEFAULT_PASSWORD = "admin123"
MIN_PASSWORD = 8
ROLES = {"admin": "Administrador", "supervisor": "Supervisor", "consulta": "Consulta"}
LEAVE_KINDS = ["Vacaciones", "Permiso médico", "Permiso personal", "Licencia", "Comisión de servicio", "Otro"]
poller = devices.Poller()


@app.on_event("startup")
def _start():
    poller.start()


# ------------------------------------------------------------------ auth helpers
class Redirect(Exception):
    def __init__(self, url):
        self.url = url


@app.exception_handler(Redirect)
async def _redir(request, exc):
    return RedirectResponse(exc.url, status_code=303)


def user(request: Request, *roles):
    uid = request.session.get("uid")
    u = db.q1("SELECT * FROM users WHERE id=? AND active=1", (uid,)) if uid else None
    if not u or request.session.get("pv") != seguridad.password_tag(SECRET, u["password_hash"]):
        # sin sesión, o la contraseña cambió desde que entró: hay que volver a ingresar
        request.session.pop("uid", None)
        raise Redirect("/login")
    if request.session.get("cambiar_clave") and request.url.path not in ("/cuenta", "/logout"):
        flash(request, "Antes de seguir, cambia la contraseña por defecto.", "warning")
        raise Redirect("/cuenta")
    if roles and u["role"] not in roles:
        flash(request, "No tienes permiso para esa sección.", "danger")
        raise Redirect("/")
    return u


def flash(request, msg, kind="success"):
    # se reasigna (no .append) para que la sesión siempre se dé por modificada y el aviso no se pierda
    request.session["flash"] = request.session.get("flash", []) + [[kind, msg]]


def render(request, name, u, **ctx):
    msgs = request.session.pop("flash", [])
    return templates.TemplateResponse(request, name, {
        "u": u, "flashes": msgs, "roles": ROLES, "company": db.get_setting("company", ""),
        "path": request.url.path, **ctx})


def back(url):
    return RedirectResponse(url, status_code=303)


async def form(request):
    return {k: (v.strip() if isinstance(v, str) else v) for k, v in (await request.form()).multi_items()}


# ------------------------------------------------------------------ login
@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None, "company": db.get_setting("company"),
                                                              "flashes": request.session.pop("flash", [])})


_DUMMY_HASH = db.hash_password(uuid.uuid4().hex)


@app.post("/login")
async def login(request: Request):
    f = await form(request)
    username, ip = f.get("username", "")[:64], seguridad.client_ip(request)
    error = lambda msg, status=200: templates.TemplateResponse(
        request, "login.html", {"error": msg, "company": db.get_setting("company")}, status_code=status)
    wait = seguridad.login_blocked(ip, username)
    if wait:
        return error(f"Demasiados intentos fallidos. Espera {wait} minutos y vuelve a intentar.", 429)
    u = db.q1("SELECT * FROM users WHERE username=? AND active=1", (username,))
    # Se compara igual aunque el usuario no exista, para no delatar qué usuarios existen
    good = await run_in_threadpool(db.check_password, f.get("password", ""), u["password_hash"] if u else _DUMMY_HASH)
    if not u or not good:
        seguridad.login_failed(ip, username)
        db.audit(username, "ingreso fallido", ip)
        await asyncio.sleep(1)
        return error("Usuario o contraseña incorrectos")
    seguridad.login_ok(ip, username)
    request.session.clear()
    request.session["uid"] = u["id"]
    request.session["pv"] = seguridad.password_tag(SECRET, u["password_hash"])
    db.audit(u["username"], "login", ip)
    if f.get("password") == DEFAULT_PASSWORD:
        request.session["cambiar_clave"] = True
        return back("/cuenta")
    return back("/")


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return back("/login")


@app.get("/cuenta", response_class=HTMLResponse)
def account(request: Request):
    return render(request, "account.html", user(request))


@app.post("/cuenta")
async def account_save(request: Request):
    u, f = user(request), await form(request)
    if not db.check_password(f.get("current", ""), u["password_hash"]):
        flash(request, "La contraseña actual no es correcta.", "danger")
    elif len(f.get("new", "")) < MIN_PASSWORD or f["new"] != f.get("confirm"):
        flash(request, f"La nueva contraseña debe tener {MIN_PASSWORD}+ caracteres y coincidir.", "danger")
    elif f["new"] == DEFAULT_PASSWORD or f["new"] == f.get("current"):
        flash(request, "Elige una contraseña distinta a la anterior.", "danger")
    else:
        new_hash = db.hash_password(f["new"])
        db.ex("UPDATE users SET password_hash=? WHERE id=?", (new_hash, u["id"]))
        # Esta sesión sigue abierta; las demás sesiones de este usuario se cierran
        request.session["pv"] = seguridad.password_tag(SECRET, new_hash)
        request.session.pop("cambiar_clave", None)
        db.audit(u["username"], "cambio de contraseña")
        flash(request, "Contraseña actualizada.")
    return back("/cuenta")


# ------------------------------------------------------------------ dashboard
TREND_KEYS = {"Puntual": "puntual", "Tarde": "tarde", "Ausente": "ausente"}


def trend_data(days: int):
    """Puntuales, tardanzas y faltas de los últimos `days` días (0 = últimas 24 h, por hora),
    más los totales del periodo anterior para comparar."""
    today = date.today()
    if not days:
        now = datetime.now().replace(minute=0, second=0, microsecond=0)
        first = now - timedelta(hours=23)
        by_hour = defaultdict(lambda: {"puntual": 0, "tarde": 0, "ausente": 0})
        rows = attendance.compute(today - timedelta(days=2), today)
        for r in rows:
            if r["entry"] and r["status"] in ("Puntual", "Tarde"):
                by_hour[r["entry"].replace(minute=0, second=0, microsecond=0)][TREND_KEYS[r["status"]]] += 1
        series = [{"date": h.strftime("%H:00"), "dow": "", **by_hour[h]} for h in (first + timedelta(hours=i) for i in range(24))]
        total = {k: sum(x[k] for x in series) for k in ("puntual", "tarde")}
        prev = {k: sum(by_hour[first - timedelta(hours=i)][k] for i in range(1, 25)) for k in ("puntual", "tarde")}
        # las faltas no tienen hora: se comparan las de hoy con las de ayer
        total["ausente"] = sum(r["status"] == "Ausente" for r in rows if r["date"] == today)
        prev["ausente"] = sum(r["status"] == "Ausente" for r in rows if r["date"] == today - timedelta(days=1))
        return {"days": 0, "series": series, "total": total, "prev": prev}
    start = today - timedelta(days=days - 1)
    by_day = defaultdict(lambda: {"puntual": 0, "tarde": 0, "ausente": 0})
    for r in attendance.compute(start - timedelta(days=days), today):
        if r["status"] in TREND_KEYS:
            by_day[r["date"]][TREND_KEYS[r["status"]]] += 1
    series = [{"date": d.strftime("%d/%m"), "dow": "LMMJVSD"[d.weekday()], **by_day[d]}
              for d in (start + timedelta(days=i) for i in range(days))]
    total = {k: sum(x[k] for x in series) for k in ("puntual", "tarde", "ausente")}
    prev = {k: sum(by_day[start - timedelta(days=i)][k] for i in range(1, days + 1)) for k in ("puntual", "tarde", "ausente")}
    return {"days": days, "series": series, "total": total, "prev": prev}


def sedes():
    """Locales ya escritos en equipos y departamentos (para sugerirlos al escribir)."""
    return [r["s"] for r in db.q("""SELECT DISTINCT trim(sede) s FROM (SELECT sede FROM devices UNION ALL
                                    SELECT sede FROM departments) WHERE COALESCE(trim(sede),'')<>'' ORDER BY 1""")]


def _mark_away(evs):
    """Marca los eventos hechos en un equipo de otro local que el del departamento del empleado."""
    for ev in evs:
        ev["away"] = ev.get("dev_sede") if attendance.away_from(ev.get("dep_sede"), ev.get("dev_sede")) else None
    return evs


def dashboard_data(days: int = 7):
    today = date.today()
    rows = attendance.compute(today, today)
    count = lambda st: sum(1 for r in rows if r["status"] == st)
    fails = ",".join(map(str, sorted(MINOR_FAIL)))
    last = db.q(f"""SELECT ev.*, e.name AS emp_name, e.id AS emp_id, d.name AS device, d.sede AS dev_sede,
                   dp.sede AS dep_sede, dp.name AS department FROM events ev
                   LEFT JOIN employees e ON e.employee_no=ev.employee_no LEFT JOIN devices d ON d.id=ev.device_id
                   LEFT JOIN departments dp ON dp.id=e.department_id
                   WHERE ev.employee_no IS NOT NULL OR ev.minor IN ({fails})
                   ORDER BY ev.event_time DESC, ev.id DESC LIMIT 15""")
    for ev in _mark_away(last):
        ev["minor_name"] = MINOR_NAMES.get(ev["minor"], f"Evento {ev['minor']}") if ev["source"] == "equipo" else "Marca manual"
    devs = db.q("SELECT id, name, online, last_seen, last_error FROM devices WHERE active=1")
    return {"total": db.q1("SELECT COUNT(*) n FROM employees WHERE active=1")["n"],
            "puntual": count("Puntual"), "tarde": count("Tarde"), "ausente": count("Ausente"),
            "pendiente": count("Pendiente"), "justificado": count("Justificado"),
            "presentes": sum(1 for r in rows if r["entry"]),
            "late_list": [{"name": r["name"], "entry": r["entry"].strftime("%H:%M"), "late": r["late_min"]}
                          for r in rows if r["status"] == "Tarde"],
            "absent_list": [r["name"] for r in rows if r["status"] in ("Ausente", "Pendiente")],
            "away_list": [{"name": r["name"], "department": r["department"], "home": r["home_sede"],
                           "sedes": ", ".join(r["away"]), "back": not attendance.away_from(r["home_sede"], r["trail"][-1][0]),
                           "trail": [{"sede": s, "time": t.strftime("%H:%M")} for s, t in r["trail"]]}
                          for r in rows if r["away"]],
            "trend": trend_data(days), "events": last, "devices": devs, "now": datetime.now().strftime("%H:%M:%S")}


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return render(request, "dashboard.html", user(request), d=dashboard_data())


@app.get("/api/dashboard")
def api_dashboard(request: Request, dias: int = 7):
    user(request)
    return JSONResponse(dashboard_data(dias if dias in (0, 7, 30) else 7))


# ------------------------------------------------------------------ empleados
def emp_lookups():
    return {"departments": db.q("SELECT * FROM departments ORDER BY name"),
            "schedules": db.q("SELECT * FROM schedules ORDER BY name")}


@app.get("/empleados", response_class=HTMLResponse)
def employees(request: Request, q: str = "", dep: str = "", inactivos: str = ""):
    u = user(request)
    where, args = ["1=1"], []
    if not inactivos:
        where.append("e.active=1")
    if q:
        where.append("(e.name LIKE ? OR e.employee_no LIKE ? OR e.document LIKE ?)")
        args += [f"%{q}%"] * 3
    if dep:
        where.append("e.department_id=?")
        args.append(dep)
    emps = db.q(f"""SELECT e.*, d.name AS department, s.name AS schedule,
                    (SELECT COUNT(*) FROM employee_sync x WHERE x.employee_id=e.id AND x.status='ok') AS sync_ok,
                    (SELECT COUNT(*) FROM employee_sync x WHERE x.employee_id=e.id AND x.status='error') AS sync_err
                    FROM employees e LEFT JOIN departments d ON d.id=e.department_id
                    LEFT JOIN schedules s ON s.id=e.schedule_id WHERE {' AND '.join(where)} ORDER BY e.name""", args)
    return render(request, "employees.html", u, emps=emps, q=q, dep=dep, inactivos=inactivos,
                  devs=db.q("SELECT id, name, host FROM devices WHERE active=1"), **emp_lookups())


@app.get("/empleados/nuevo", response_class=HTMLResponse)
def employee_new(request: Request):
    u = user(request, "admin", "supervisor")
    nxt = db.q1("SELECT MAX(CAST(employee_no AS INTEGER)) m FROM employees")["m"] or 0
    return render(request, "employee_form.html", u, e={"employee_no": str(nxt + 1), "active": 1}, sync=[],
                  **emp_lookups())


@app.get("/empleados/{eid}", response_class=HTMLResponse)
def employee_edit(request: Request, eid: int):
    u = user(request, "admin", "supervisor")
    e = db.q1("SELECT * FROM employees WHERE id=?", (eid,))
    if not e:
        raise Redirect("/empleados")
    sync = db.q("""SELECT s.*, d.name AS device FROM employee_sync s JOIN devices d ON d.id=s.device_id
                   WHERE s.employee_id=?""", (eid,))
    return render(request, "employee_form.html", u, e=e, sync=sync, **emp_lookups())


@app.post("/empleados/guardar")
async def employee_save(request: Request):
    u = user(request, "admin", "supervisor")
    raw = await request.form(max_part_size=8 * 1024 * 1024)  # photo_data (foto en texto) puede pasar de 1 MB
    f = {k: (v.strip() if isinstance(v, str) else v) for k, v in raw.items()}
    eid = int(f["id"]) if f.get("id") else None
    no = f.get("employee_no", "")
    if not re.fullmatch(r"[A-Za-z0-9]{1,32}", no) or not f.get("name"):
        flash(request, "El N° de empleado (solo letras/números) y el nombre son obligatorios.", "danger")
        return back(f"/empleados/{eid}" if eid else "/empleados/nuevo")
    dup = db.q1("SELECT id FROM employees WHERE employee_no=? AND id<>?", (no, eid or 0))
    if dup:
        flash(request, f"Ya existe un empleado con el N° {no}.", "danger")
        return back(f"/empleados/{eid}" if eid else "/empleados/nuevo")

    photo_name = None
    data = raw.get("photo_data")
    upload = next((raw.get(k) for k in ("photo", "photo_cam") if getattr(raw.get(k), "filename", "")), None)
    if (isinstance(data, str) and data) or upload is not None:
        try:
            if isinstance(data, str) and data:
                # Foto ya achicada en el navegador: "data:image/jpeg;base64,..."
                content = base64.b64decode(data.split(",", 1)[-1])
            else:
                content = await upload.read()
            jpeg = devices.prepare_face(content)
        except Exception:
            flash(request, "La foto no es una imagen válida.", "danger")
            return back(f"/empleados/{eid}" if eid else "/empleados/nuevo")
        photo_name = f"{no}_{uuid.uuid4().hex[:8]}.jpg"
        (db.PHOTOS_DIR / photo_name).write_bytes(jpeg)

    vals = [no, f["name"], f.get("document"), f.get("department_id") or None, f.get("position"), f.get("email"),
            f.get("phone"), f.get("gender") or "unknown", f.get("schedule_id") or None, f.get("hire_date") or None,
            1 if f.get("active") else 0]
    renamed = None
    if eid:
        old = db.q1("SELECT * FROM employees WHERE id=?", (eid,))
        if old["employee_no"] != no:
            renamed = old
            # Sus marcas anteriores pasan al N° nuevo, para que el historial siga junto
            db.ex("UPDATE events SET employee_no=? WHERE employee_no=?", (no, old["employee_no"]))
        db.ex("""UPDATE employees SET employee_no=?, name=?, document=?, department_id=?, position=?, email=?,
                 phone=?, gender=?, schedule_id=?, hire_date=?, active=? WHERE id=?""", vals + [eid])
        if photo_name:
            db.ex("UPDATE employees SET photo=?, face_at=COALESCE(face_at, ?) WHERE id=?",
                  (photo_name, date.today().isoformat(), eid))
            if old["photo"]:
                (db.PHOTOS_DIR / old["photo"]).unlink(missing_ok=True)
    else:
        eid = db.ex("""INSERT INTO employees(employee_no, name, document, department_id, position, email, phone,
                       gender, schedule_id, hire_date, active, photo, face_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    vals + [photo_name, date.today().isoformat() if photo_name else None])
    db.audit(u["username"], "guardar empleado", f"{no} {f['name']}")
    emp = db.q1("SELECT * FROM employees WHERE id=?", (eid,))
    if renamed:
        # Se borra del equipo el usuario con el N° viejo; si no, el equipo lo seguiría reconociendo
        # y volvería a aparecer aquí como otro empleado
        for name, ok, m in await run_in_threadpool(devices.remove_employee, renamed):
            if not ok:
                flash(request, f"{name}: no se pudo borrar el N° viejo {renamed['employee_no']} del equipo ({m}). "
                               "Bórralo a mano en el equipo.", "warning")
        db.audit(u["username"], "cambio de N° de empleado", f"{renamed['employee_no']} -> {no}")
    if emp["active"]:
        res = await run_in_threadpool(devices.push_employee, emp)
    else:
        res = await run_in_threadpool(devices.remove_employee, emp)
    _flash_results(request, "Empleado guardado.", res)
    return back(f"/empleados/{eid}")


def _flash_results(request, msg, res):
    if not res:
        flash(request, msg + " (No hay equipos activos configurados.)", "warning")
    for name, ok, m in res:
        flash(request, f"{msg} {name}: {m}", "success" if ok else "danger")
        msg = ""


@app.post("/empleados/{eid}/sincronizar")
async def employee_sync(request: Request, eid: int):
    user(request, "admin", "supervisor")
    emp = db.q1("SELECT * FROM employees WHERE id=?", (eid,))
    _flash_results(request, "", await run_in_threadpool(devices.push_employee, emp))
    return back(f"/empleados/{eid}")


@app.post("/empleados/{eid}/eliminar")
async def employee_delete(request: Request, eid: int):
    u = user(request, "admin")
    emp = db.q1("SELECT * FROM employees WHERE id=?", (eid,))
    res = await run_in_threadpool(devices.remove_employee, emp)
    # Se conserva el historial de marcas; el empleado queda inactivo
    db.ex("UPDATE employees SET active=0 WHERE id=?", (eid,))
    db.audit(u["username"], "desactivar empleado", f"{emp['employee_no']} {emp['name']}")
    _flash_results(request, "Empleado desactivado y retirado de los equipos.", res)
    return back("/empleados")


@app.post("/empleados/sincronizar-todos")
async def employees_sync_all(request: Request):
    u = user(request, "admin")
    ok = err = 0
    for emp in db.q("SELECT * FROM employees WHERE active=1"):
        for _, good, _m in await run_in_threadpool(devices.push_employee, emp):
            ok, err = ok + good, err + (not good)
    db.audit(u["username"], "sincronizar todos")
    flash(request, f"Sincronización terminada: {ok} correctas, {err} con error.", "success" if not err else "warning")
    return back("/empleados")


@app.post("/empleados/importar-equipo")
async def employees_import_device(request: Request):
    u = user(request, "admin")
    f = await form(request)
    dev = db.q1("SELECT * FROM devices WHERE id=?", (f.get("device_id"),))
    if not dev:
        flash(request, "Elige un equipo.", "danger")
        return back("/empleados")
    try:
        users = await run_in_threadpool(devices.client(dev).search_users)
    except ISAPIError as e:
        flash(request, str(e), "danger")
        return back("/empleados")
    sched = db.q1("SELECT id FROM schedules ORDER BY id")
    n = 0
    for us in users:
        if not db.q1("SELECT 1 FROM employees WHERE employee_no=?", (us["employeeNo"],)):
            db.ex("INSERT INTO employees(employee_no, name, gender, schedule_id) VALUES (?,?,?,?)",
                  (us["employeeNo"], us.get("name") or us["employeeNo"], us.get("gender", "unknown"),
                   sched["id"] if sched else None))
            n += 1
    db.audit(u["username"], "importar del equipo", f"{dev['name']}: {n}")
    flash(request, f"Se importaron {n} empleados nuevos desde {dev['name']} ({len(users)} en el equipo). "
                   "Las fotos se quedan en el equipo; súbelas aquí si quieres verlas en el sistema.")
    return back("/empleados")


@app.post("/empleados/importar-excel")
async def employees_import_excel(request: Request):
    u = user(request, "admin")
    raw = await request.form()
    try:
        wb = load_workbook(io.BytesIO(await raw["file"].read()), read_only=True)
    except Exception:
        flash(request, "No se pudo leer el archivo. Usa un .xlsx.", "danger")
        return back("/empleados")
    rows = list(wb.active.iter_rows(values_only=True))
    if not rows:
        flash(request, "El Excel está vacío.", "danger")
        return back("/empleados")
    head = [str(h or "").strip().lower() for h in rows[0]]
    col = lambda *names: next((head.index(n) for n in names if n in head), None)
    c_no, c_name = col("numero", "número", "n°", "codigo", "código", "employee_no"), col("nombre", "name")
    c_doc, c_dep, c_pos = col("documento", "dni", "cedula", "cédula"), col("departamento", "area", "área"), col("cargo", "puesto")
    c_mail, c_tel = col("email", "correo"), col("telefono", "teléfono", "celular")
    if c_no is None or c_name is None:
        flash(request, "El Excel debe tener columnas 'numero' y 'nombre'.", "danger")
        return back("/empleados")
    sched = db.q1("SELECT id FROM schedules ORDER BY id")
    n = 0
    for r in rows[1:]:
        g = lambda c: (str(r[c]).strip() if c is not None and r[c] is not None else None)
        no, name = g(c_no), g(c_name)
        if not no or not name:
            continue
        no = no.removesuffix(".0")
        dep_id = None
        if g(c_dep):
            db.ex("INSERT OR IGNORE INTO departments(name) VALUES (?)", (g(c_dep),))
            dep_id = db.q1("SELECT id FROM departments WHERE name=?", (g(c_dep),))["id"]
        if db.q1("SELECT 1 FROM employees WHERE employee_no=?", (no,)):
            db.ex("UPDATE employees SET name=?, document=COALESCE(?,document), department_id=COALESCE(?,department_id), "
                  "position=COALESCE(?,position), email=COALESCE(?,email), phone=COALESCE(?,phone) WHERE employee_no=?",
                  (name, g(c_doc), dep_id, g(c_pos), g(c_mail), g(c_tel), no))
        else:
            db.ex("INSERT INTO employees(employee_no,name,document,department_id,position,email,phone,schedule_id) "
                  "VALUES (?,?,?,?,?,?,?,?)", (no, name, g(c_doc), dep_id, g(c_pos), g(c_mail), g(c_tel),
                                               sched["id"] if sched else None))
        n += 1
    db.audit(u["username"], "importar excel", str(n))
    flash(request, f"{n} empleados importados/actualizados. Usa 'Sincronizar todos' para enviarlos a los equipos.")
    return back("/empleados")


@app.get("/fotos/{name}")
def photo(request: Request, name: str):
    user(request)
    p = db.PHOTOS_DIR / Path(name).name
    return FileResponse(p) if p.exists() else Response(status_code=404)


def _face_from_devices(emp):
    """Trae la foto de rostro registrada en el equipo; si no hay, la última captura de una marcación."""
    devs = db.q("SELECT * FROM devices WHERE active=1")
    for dev in devs:
        try:
            c = devices.client(dev)
            url = c.face_url(emp["employee_no"])
            if url:
                return c.fetch_picture(url), True
        except ISAPIError:
            pass
    ev = db.q1("""SELECT ev.picture_url, d.* FROM events ev JOIN devices d ON d.id=ev.device_id
                  WHERE ev.employee_no=? AND ev.picture_url IS NOT NULL AND ev.valid=1
                  ORDER BY ev.event_time DESC LIMIT 1""", (emp["employee_no"],))
    if ev:
        try:
            return devices.client(ev).fetch_picture(ev["picture_url"]), False
        except ISAPIError:
            pass
    return None, False


@app.get("/empleados/{eid}/rostro")
async def employee_face(request: Request, eid: int):
    """Foto del empleado: la del sistema o, si no tiene, la que está en el equipo (se guarda para la próxima)."""
    user(request)
    emp = db.q1("SELECT * FROM employees WHERE id=?", (eid,))
    if not emp:
        return Response(status_code=404)
    if emp["photo"] and (db.PHOTOS_DIR / emp["photo"]).exists():
        return FileResponse(db.PHOTOS_DIR / emp["photo"])
    data, enrolled = await run_in_threadpool(_face_from_devices, emp)
    if not data:
        return Response(status_code=404)
    if enrolled:
        name = f"{emp['employee_no']}_{uuid.uuid4().hex[:8]}.jpg"
        (db.PHOTOS_DIR / name).write_bytes(data)
        db.ex("UPDATE employees SET photo=? WHERE id=? AND photo IS NULL", (name, eid))
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


# ------------------------------------------------------------------ departamentos / horarios / feriados
@app.get("/configuracion", response_class=HTMLResponse)
def config(request: Request):
    u = user(request, "admin", "supervisor")
    return render(request, "config.html", u, departments=db.q("SELECT * FROM departments ORDER BY name"),
                  sedes=sedes(),
                  schedules=db.q("SELECT * FROM schedules ORDER BY name"),
                  holidays=db.q("SELECT * FROM holidays ORDER BY date DESC"),
                  settings={k: db.get_setting(k) for k in ("company", "poll_seconds", "min_gap_minutes")})


@app.post("/configuracion/{what}")
async def config_save(request: Request, what: str):
    u = user(request, "admin", "supervisor")
    f = await form(request)
    if what == "departamento" and f.get("name"):
        db.ex("INSERT OR IGNORE INTO departments(name, sede) VALUES (?,?)", (f["name"], f.get("sede") or None))
    elif what == "departamento-sede" and f.get("id"):
        db.ex("UPDATE departments SET sede=? WHERE id=?", (f.get("sede") or None, f["id"]))
    elif what == "departamento-eliminar":
        db.ex("UPDATE employees SET department_id=NULL WHERE department_id=?", (f["id"],))
        db.ex("DELETE FROM departments WHERE id=?", (f["id"],))
    elif what == "horario":
        if not (f.get("name") and f.get("start_time") and f.get("end_time")):
            flash(request, "El horario necesita nombre, hora de entrada y hora de salida.", "danger")
            return back("/configuracion")
        if not all((f.get(k) or "0").isdigit() for k in ("tolerance_min", "lunch_min")):
            flash(request, "La tolerancia y el almuerzo deben ser minutos (números enteros).", "danger")
            return back("/configuracion")
        days = ",".join(sorted(v for k, v in (await request.form()).multi_items() if k == "days"))
        win = tuple(f.get(k) or None for k in ("in_from", "in_to", "out_from", "out_to"))
        if any(win) and not all(win):
            flash(request, "Completa las cuatro horas de las ventanas, o déjalas todas vacías para no usar ventanas.", "danger")
            return back("/configuracion")
        if all(win) and (win[0] > win[1] or win[2] > win[3]):
            flash(request, "En cada ventana la hora «desde» debe ser anterior a la hora «hasta».", "danger")
            return back("/configuracion")
        old = db.q1("SELECT * FROM schedules WHERE id=?", (f.get("id") or 0,))
        before = (old["start_time"], old["end_time"]) if old else ("08:00", "17:00")
        if all(win) and win == db.default_windows(*before) and before != (f["start_time"], f["end_time"]):
            # Las ventanas estaban en su valor automático: se mueven con las nuevas horas del horario
            win = db.default_windows(f["start_time"], f["end_time"])
        vals = (f["name"], f["start_time"], f["end_time"], int(f.get("tolerance_min") or 0), int(f.get("lunch_min") or 0),
                days) + win
        if f.get("id"):
            db.ex("""UPDATE schedules SET name=?, start_time=?, end_time=?, tolerance_min=?, lunch_min=?, days=?,
                     in_from=?, in_to=?, out_from=?, out_to=? WHERE id=?""", vals + (f["id"],))
        else:
            db.ex("""INSERT INTO schedules(name,start_time,end_time,tolerance_min,lunch_min,days,in_from,in_to,out_from,out_to)
                     VALUES (?,?,?,?,?,?,?,?,?,?)""", vals)
    elif what == "horario-eliminar":
        db.ex("UPDATE employees SET schedule_id=NULL WHERE schedule_id=?", (f["id"],))
        db.ex("DELETE FROM schedules WHERE id=?", (f["id"],))
    elif what == "feriado" and f.get("date"):
        db.ex("INSERT OR REPLACE INTO holidays(date, name) VALUES (?,?)", (f["date"], f.get("name")))
    elif what == "feriado-eliminar":
        db.ex("DELETE FROM holidays WHERE id=?", (f["id"],))
    elif what == "general" and u["role"] == "admin":
        limits = {"poll_seconds": (2, 300, "La revisión de respaldo debe ser de 2 a 300 segundos."),
                  "min_gap_minutes": (0, 60, "La separación mínima debe ser de 0 a 60 minutos.")}
        for k, (low, high, msg) in limits.items():
            if k in f and not (f[k].isdigit() and low <= int(f[k]) <= high):
                flash(request, msg, "danger")
                return back("/configuracion")
        for k in ("company", "poll_seconds", "min_gap_minutes"):
            if k in f:
                db.set_setting(k, f[k])
    db.audit(u["username"], f"configuración: {what}", str({k: v for k, v in f.items() if k != "password"}))
    flash(request, "Cambios guardados.")
    return back("/configuracion")


@app.get("/respaldo")
def backup_download(request: Request):
    u = user(request, "admin")
    data = db.backup_zip()
    db.audit(u["username"], "respaldo descargado", f"{len(data) // 1024} KB")
    name = f"respaldo-paso-largo-{datetime.now():%Y%m%d-%H%M}.zip"
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.post("/respaldo/restaurar")
async def backup_restore(request: Request):
    u = user(request, "admin")
    f = await request.form()
    up = f.get("archivo")
    if not up or not getattr(up, "filename", ""):
        flash(request, "Elige el archivo .zip del respaldo.", "danger")
        return back("/configuracion")
    raw = await up.read()
    # Copia de lo que hay ahora, para poder deshacer la restauración
    saved = db.DATA_DIR / "respaldos" / f"antes-de-restaurar-{datetime.now():%Y%m%d-%H%M%S}.zip"
    saved.parent.mkdir(exist_ok=True)
    saved.write_bytes(await run_in_threadpool(db.backup_zip))
    try:
        emps, evs = await run_in_threadpool(db.restore_zip, raw)
    except ValueError as e:
        flash(request, str(e), "danger")
        return back("/configuracion")
    db.audit(u["username"], "respaldo restaurado", f"{up.filename}: {emps} empleados, {evs} eventos; "
                                                   f"lo anterior quedó en datos/respaldos/{saved.name}")
    poller.wake.set()
    # Los usuarios ahora son los del respaldo: se pide entrar de nuevo
    request.session.clear()
    flash(request, f"Respaldo restaurado: {emps} empleados y {evs} marcaciones. Entra con un usuario del respaldo.")
    return back("/login")


# ------------------------------------------------------------------ permisos / justificaciones
@app.get("/permisos", response_class=HTMLResponse)
def leaves(request: Request):
    u = user(request, "admin", "supervisor")
    rows = db.q("""SELECT l.*, e.name, e.employee_no FROM leaves l JOIN employees e ON e.id=l.employee_id
                   ORDER BY l.date_from DESC LIMIT 300""")
    return render(request, "leaves.html", u, rows=rows, kinds=LEAVE_KINDS,
                  emps=db.q("SELECT id, name, employee_no FROM employees WHERE active=1 ORDER BY name"))


@app.post("/permisos")
async def leave_save(request: Request):
    u = user(request, "admin", "supervisor")
    f = await form(request)
    if f.get("delete"):
        db.ex("DELETE FROM leaves WHERE id=?", (f["delete"],))
    elif f.get("employee_id") and f.get("date_from"):
        if (f.get("date_to") or f["date_from"]) < f["date_from"]:
            flash(request, "La fecha «hasta» no puede ser anterior a «desde».", "danger")
            return back("/permisos")
        db.ex("INSERT INTO leaves(employee_id,date_from,date_to,kind,notes,created_by) VALUES (?,?,?,?,?,?)",
              (f["employee_id"], f["date_from"], f.get("date_to") or f["date_from"], f.get("kind"), f.get("notes"),
               u["username"]))
    db.audit(u["username"], "permisos", str(f))
    flash(request, "Guardado.")
    return back("/permisos")


# ------------------------------------------------------------------ asistencia
@app.get("/asistencia", response_class=HTMLResponse)
def attendance_day(request: Request, fecha: str = "", dep: str = ""):
    u = user(request)
    d = attendance.parse_date(fecha, date.today())
    rows = attendance.compute(d, d, department_id=dep or None)
    return render(request, "attendance.html", u, rows=rows, d=d, dep=dep, prev=d - timedelta(days=1),
                  next=d + timedelta(days=1), departments=db.q("SELECT * FROM departments ORDER BY name"),
                  emps=db.q("SELECT id, name, employee_no FROM employees WHERE active=1 ORDER BY name"))


@app.post("/asistencia/manual")
async def manual_mark(request: Request):
    u = user(request, "admin", "supervisor")
    f = await form(request)
    emp = db.q1("SELECT * FROM employees WHERE id=?", (f.get("employee_id"),))
    if emp and f.get("date") and f.get("time"):
        t = f"{f['date']}T{f['time']}:00"
        db.ex("""INSERT INTO events(device_id, serial_no, event_time, employee_no, name, major, minor, verify_mode,
                 attendance_status, valid, source) VALUES (NULL, NULL, ?, ?, ?, 0, 0, 'manual', ?, 1, 'manual')""",
              (t, emp["employee_no"], emp["name"], f.get("kind") or None))
        db.audit(u["username"], "marca manual", f"{emp['employee_no']} {t} {f.get('kind')} {f.get('reason')}")
        flash(request, f"Marca manual registrada para {emp['name']}.")
    return back(f"/asistencia?fecha={f.get('date', '')}")


@app.get("/eventos", response_class=HTMLResponse)
def events(request: Request, desde: str = "", hasta: str = "", emp: str = "", dev: str = "", todos: str = ""):
    u = user(request)
    d1 = attendance.parse_date(desde, date.today())
    d2 = attendance.parse_date(hasta, d1)
    where, args = ["ev.event_time >= ?", "ev.event_time < ?"], [d1.isoformat(), (d2 + timedelta(days=1)).isoformat()]
    if emp:
        where.append("ev.employee_no=?")
        args.append(emp)
    if dev:
        where.append("ev.device_id=?")
        args.append(dev)
    if not todos:
        where.append("ev.employee_no IS NOT NULL")
    rows = db.q(f"""SELECT ev.*, e.name AS emp_name, e.id AS emp_id, d.name AS device, d.sede AS dev_sede,
                    dp.sede AS dep_sede, dp.name AS department, s.in_from, s.in_to, s.out_from, s.out_to FROM events ev
                    LEFT JOIN employees e ON e.employee_no=ev.employee_no LEFT JOIN devices d ON d.id=ev.device_id
                    LEFT JOIN departments dp ON dp.id=e.department_id
                    LEFT JOIN schedules s ON s.id=e.schedule_id
                    WHERE {' AND '.join(where)} ORDER BY ev.event_time DESC LIMIT 1000""", args)
    roles = attendance.mark_roles(d1, d2)
    for ev in _mark_away(rows):
        ev["counts_as"] = (roles.get((ev["employee_no"], datetime.fromisoformat(ev["event_time"])), "No cuenta")
                           if ev["valid"] else "")
    return render(request, "events.html", u, rows=rows, desde=d1, hasta=d2, emp=emp, dev=dev, todos=todos,
                  emps=db.q("SELECT name, employee_no FROM employees ORDER BY name"),
                  devs=db.q("SELECT id, name FROM devices ORDER BY name"))


@app.post("/eventos/{evid}/eliminar")
async def event_delete(request: Request, evid: int):
    """Solo se pueden borrar las marcas manuales; las del equipo son el registro original."""
    u = user(request, "admin", "supervisor")
    ev = db.q1("SELECT * FROM events WHERE id=? AND source='manual'", (evid,))
    if ev:
        db.ex("DELETE FROM events WHERE id=?", (evid,))
        db.audit(u["username"], "borrar marca manual", f"{ev['employee_no']} {ev['event_time']} {ev['attendance_status']}")
        flash(request, "Marca manual borrada.")
    return back(seguridad.safe_back(request.headers.get("referer"), "/eventos"))


@app.get("/eventos/{evid}/foto")
async def event_photo(request: Request, evid: int):
    user(request)
    ev = db.q1("SELECT * FROM events WHERE id=?", (evid,))
    dev = db.q1("SELECT * FROM devices WHERE id=?", (ev["device_id"],)) if ev else None
    if not dev or not ev["picture_url"]:
        return Response(status_code=404)
    try:
        data = await run_in_threadpool(devices.client(dev).fetch_picture, ev["picture_url"])
    except ISAPIError:
        return Response(status_code=502)
    # La foto de una marcación no cambia: el navegador la guarda y no se vuelve a pedir al equipo
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=604800"})


# ------------------------------------------------------------------ reportes
def report_args(desde, hasta, emp, dep):
    today = date.today()
    d1 = attendance.parse_date(desde, today.replace(day=1))
    d2 = attendance.parse_date(hasta, today)
    rows = attendance.compute(d1, d2, employee_id=emp or None, department_id=dep or None)
    return d1, d2, rows, attendance.summarize(rows)


@app.get("/reportes", response_class=HTMLResponse)
def report(request: Request, desde: str = "", hasta: str = "", emp: str = "", dep: str = "", vista: str = "resumen"):
    u = user(request)
    d1, d2, rows, summary = report_args(desde, hasta, emp, dep)
    return render(request, "reports.html", u, d1=d1, d2=d2, emp=emp, dep=dep, vista=vista, rows=rows,
                  summary=summary, departments=db.q("SELECT * FROM departments ORDER BY name"),
                  emps=db.q("SELECT id, name FROM employees ORDER BY name"), qs=request.url.query)


@app.get("/reportes/exportar.{fmt}")
def report_export(request: Request, fmt: str, desde: str = "", hasta: str = "", emp: str = "", dep: str = ""):
    u = user(request)
    d1, d2, rows, summary = report_args(desde, hasta, emp, dep)
    title = "Reporte de asistencia"
    period = f"Del {d1.strftime('%d/%m/%Y')} al {d2.strftime('%d/%m/%Y')}"
    fname = f"asistencia_{d1.isoformat()}_{d2.isoformat()}"
    db.audit(u["username"], f"exportar {fmt}", period)
    if fmt == "xlsx":
        return Response(reports.to_excel(rows, summary, title, period),
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="{fname}.xlsx"'})
    return Response(reports.to_pdf(rows, summary, title, period, db.get_setting("company", "")),
                    media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{fname}.pdf"'})


# ------------------------------------------------------------------ dispositivos
@app.get("/dispositivos", response_class=HTMLResponse)
def device_list(request: Request):
    u = user(request)
    devs = db.q("""SELECT d.*, (SELECT COUNT(*) FROM events ev WHERE ev.device_id=d.id) AS n_events
                   FROM devices d ORDER BY d.name""")
    return render(request, "devices.html", u, devs=devs, sedes=sedes())


@app.post("/dispositivos/buscar")
async def device_search(request: Request):
    """Busca biométricos en la red; a los ya registrados que cambiaron de IP se les corrige sola."""
    u = user(request, "admin")
    found, nets = await run_in_threadpool(devices.search_network)
    for f in found:
        if f["status"] == "movido":
            flash(request, f"«{f['dev_name']}» estaba en otra IP: {f['note']}. Ya se reconectó.")
    if not found:
        flash(request, f"No se encontró ningún biométrico en la red ({', '.join(nets) or 'sin red'}). "
                       "Revisa que esté encendido y conectado al mismo router/switch que esta PC.", "warning")
    db.audit(u["username"], "buscar equipos en la red", ", ".join(f["ip"] for f in found))
    poller.wake.set()
    devs = db.q("""SELECT d.*, (SELECT COUNT(*) FROM events ev WHERE ev.device_id=d.id) AS n_events
                   FROM devices d ORDER BY d.name""")
    return render(request, "devices.html", u, devs=devs, found=found, nets=nets, searched=True, sedes=sedes())


@app.get("/dispositivos/comparar", response_class=HTMLResponse)
async def device_compare(request: Request):
    """Con dos o más biométricos: revisa que cada persona tenga el mismo N° y su rostro en todos."""
    u = user(request, "admin")
    cdevs, rows, errors = await run_in_threadpool(devices.compare_users)
    devs = db.q("""SELECT d.*, (SELECT COUNT(*) FROM events ev WHERE ev.device_id=d.id) AS n_events
                   FROM devices d ORDER BY d.name""")
    names = {d["id"]: d["name"] for d in devs}
    return render(request, "devices.html", u, devs=devs, sedes=sedes(), compared=True, cdevs=cdevs, crows=rows,
                  cerrors={names.get(k, k): v for k, v in errors.items()})


@app.post("/dispositivos/copiar")
async def device_copy(request: Request):
    u = user(request, "admin")
    nos = [v for k, v in (await request.form()).multi_items() if k == "no"]
    done, msgs = await run_in_threadpool(devices.copy_to_missing, nos)
    db.audit(u["username"], "copiar rostros entre equipos", ", ".join(nos))
    if done:
        flash(request, f"Se copiaron {done} rostros a los equipos donde faltaban.")
    for m in msgs[:8]:
        flash(request, m, "warning")
    return back("/dispositivos/comparar")


@app.get("/dispositivos/{did}/rostro/{no}")
async def device_face(request: Request, did: int, no: str):
    """Foto del rostro guardado en ese equipo para ese N° (para comparar entre equipos)."""
    user(request, "admin")
    dev = db.q1("SELECT * FROM devices WHERE id=?", (did,))
    if not dev:
        return Response(status_code=404)

    def get():
        c = devices.client(dev)
        url = c.face_url(no)
        return c.fetch_picture(url) if url else None
    try:
        data = await run_in_threadpool(get)
    except ISAPIError:
        data = None
    if not data:
        return Response(status_code=404)
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=300"})


@app.post("/dispositivos/guardar")
async def device_save(request: Request):
    u = user(request, "admin")
    f = await form(request)
    if not (f.get("port") or "80").isdigit() or not f.get("host") or not f.get("name"):
        flash(request, "Revisa los datos del equipo: nombre, IP y puerto (número) son obligatorios.", "danger")
        return back("/dispositivos")
    host, port = f["host"].strip().lower(), int(f.get("port") or 80)
    vals = [f["name"], host, port, 1 if f.get("https") else 0, f["username"],
            devices.local_offset(), int(f.get("door_no") or 1), f.get("direction") or "ambos",
            1 if f.get("active") else 0, (f.get("sede") or "").strip() or None]
    # Un equipo se identifica por IP + puerto: nunca se registra dos veces
    same = db.q1("SELECT id, name FROM devices WHERE host=? AND port=? AND id<>?", (host, port, f.get("id") or 0))
    if same and f.get("id"):
        flash(request, f"La dirección {host}:{port} ya es del equipo «{same['name']}».", "danger")
        return back("/dispositivos")
    if same:
        f["id"] = str(same["id"])
        vals[-2] = 1
        flash(request, f"El equipo {host}:{port} ya estaba registrado como «{same['name']}»: se actualizaron "
                       "sus datos en lugar de crear otro.", "info")
    if f.get("id"):
        db.ex("""UPDATE devices SET name=?, host=?, port=?, https=?, username=?, tz_offset=?, door_no=?, direction=?,
                 active=?, sede=? WHERE id=?""", vals + [f["id"]])
        if f.get("password"):
            db.ex("UPDATE devices SET password=? WHERE id=?", (f["password"], f["id"]))
        did = int(f["id"])
    else:
        did = db.ex("""INSERT INTO devices(name, host, port, https, username, tz_offset, door_no, direction, active,
                       sede, password) VALUES (?,?,?,?,?,?,?,?,?,?,?)""", vals + [f.get("password", "")])
    db.audit(u["username"], "guardar dispositivo", f"{f['name']} {f['host']}")
    dev = db.q1("SELECT * FROM devices WHERE id=?", (did,))
    poller.wake.set()
    try:
        info = await run_in_threadpool(devices.refresh_info, dev)
        flash(request, f"Conectado a {info.get('deviceName') or dev['name']} — modelo {info.get('model')}, "
                       f"serie {info.get('serialNumber')}, firmware {info.get('firmwareVersion')}.")
    except ISAPIError as e:
        flash(request, f"Dispositivo guardado, pero no responde: {e}", "warning")
    return back("/dispositivos")


@app.post("/dispositivos/{did}/{action}")
async def device_action(request: Request, did: int, action: str):
    u = user(request, "admin")
    dev = db.q1("SELECT * FROM devices WHERE id=?", (did,))
    if not dev:
        flash(request, "Ese equipo ya no existe.", "danger")
        return back("/dispositivos")
    c = devices.client(dev)
    try:
        if action == "probar":
            info = await run_in_threadpool(devices.refresh_info, dev)
            n = await run_in_threadpool(c.user_count)
            t = await run_in_threadpool(c.get_time)
            flash(request, f"En línea: {info.get('model')} (serie {info.get('serialNumber')}), {n} usuarios, "
                           f"hora del equipo {t.get('localTime', '?')}.")
        elif action == "ip":
            f = await form(request)
            host, _, port = (f.get("host") or "").strip().partition(":")
            if not re.fullmatch(r"[\w.-]+", host):
                flash(request, "Escribe la IP del equipo, por ejemplo 192.168.1.200.", "danger")
                return back("/dispositivos")
            info = await run_in_threadpool(devices.connect_at, dev, host, port if port.isdigit() else None)
            poller.wake.set()
            flash(request, f"Conectado: {dev['name']} ahora está en {host.lower()} "
                           f"({info.get('model')}, serie {info.get('serialNumber')}).")
        elif action == "hora":
            await run_in_threadpool(c.set_time, datetime.now(), devices.local_offset())
            devices.CLOCK_WARNINGS.pop(did, None)
            flash(request, f"Hora de {dev['name']} sincronizada con este servidor ({datetime.now():%d/%m/%Y %H:%M}).")
        elif action == "eventos":
            dias = (await form(request)).get("dias") or "31"
            since = datetime.now() - timedelta(days=int(dias) if dias.isdigit() else 31)
            n = await run_in_threadpool(devices.pull_events, dev, since)
            flash(request, f"Descarga completa: {n} eventos nuevos.")
        elif action == "reiniciar":
            await run_in_threadpool(c.reboot)
            flash(request, f"{dev['name']} se está reiniciando.", "warning")
        elif action == "eliminar":
            db.ex("UPDATE devices SET active=0 WHERE id=?", (did,))
            devices.CLOCK_WARNINGS.pop(did, None)
            flash(request, "Dispositivo desactivado: ya no se leen sus marcas (su historial se conserva).", "warning")
        elif action == "activar":
            db.ex("UPDATE devices SET active=1 WHERE id=?", (did,))
            poller.wake.set()
            flash(request, f"{dev['name']} vuelve a estar activo.")
        db.audit(u["username"], f"dispositivo {action}", dev["name"])
    except ISAPIError as e:
        flash(request, f"{dev['name']}: {e}", "danger")
    return back(seguridad.safe_back(request.headers.get("referer"), "/dispositivos"))


# ------------------------------------------------------------------ usuarios del sistema
@app.get("/usuarios", response_class=HTMLResponse)
def users(request: Request):
    u = user(request, "admin")
    return render(request, "users.html", u, users=db.q("SELECT * FROM users ORDER BY username"),
                  log=db.q("SELECT * FROM audit ORDER BY id DESC LIMIT 200"))


@app.post("/usuarios")
async def user_save(request: Request):
    u = user(request, "admin")
    f = await form(request)
    if f.get("role", "consulta") not in ROLES:
        flash(request, "Rol no válido.", "danger")
        return back("/usuarios")
    if f.get("id"):
        if int(f["id"]) == u["id"] and (f.get("role") != "admin" or not f.get("active")):
            flash(request, "No puedes quitarte el rol de administrador ni desactivarte.", "danger")
            return back("/usuarios")
        db.ex("UPDATE users SET full_name=?, role=?, active=? WHERE id=?",
              (f.get("full_name"), f["role"], 1 if f.get("active") else 0, f["id"]))
        if f.get("password"):
            if len(f["password"]) < MIN_PASSWORD:
                flash(request, f"La contraseña debe tener {MIN_PASSWORD}+ caracteres.", "danger")
                return back("/usuarios")
            new_hash = db.hash_password(f["password"])
            db.ex("UPDATE users SET password_hash=? WHERE id=?", (new_hash, f["id"]))
            if int(f["id"]) == u["id"]:
                request.session["pv"] = seguridad.password_tag(SECRET, new_hash)
    else:
        if not f.get("username") or len(f.get("password", "")) < MIN_PASSWORD:
            flash(request, f"Usuario y contraseña ({MIN_PASSWORD}+ caracteres) son obligatorios.", "danger")
            return back("/usuarios")
        if db.q1("SELECT 1 FROM users WHERE username=?", (f["username"],)):
            flash(request, "Ese nombre de usuario ya existe.", "danger")
            return back("/usuarios")
        db.ex("INSERT INTO users(username, full_name, password_hash, role) VALUES (?,?,?,?)",
              (f["username"], f.get("full_name"), db.hash_password(f["password"]), f.get("role", "consulta")))
    db.audit(u["username"], "guardar usuario", f.get("username") or f.get("id"))
    flash(request, "Usuario guardado.")
    return back("/usuarios")
