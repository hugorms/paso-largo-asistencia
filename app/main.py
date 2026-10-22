import io
import logging
import re
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from openpyxl import load_workbook
from starlette.middleware.sessions import SessionMiddleware

from . import attendance, db, devices, reports
from .isapi import MINOR_NAMES, ISAPIError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
SECRET = db.init()

app = FastAPI(title="Paso Largo", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
app.add_middleware(SessionMiddleware, secret_key=SECRET, max_age=12 * 3600, same_site="lax")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.globals.update(local_offset=devices.local_offset, hm=attendance.hm, colors=attendance.STATUS_COLORS, minor_names=MINOR_NAMES,
                             days=reports.DAYS)

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
    if not u:
        raise Redirect("/login")
    if roles and u["role"] not in roles:
        flash(request, "No tienes permiso para esa sección.", "danger")
        raise Redirect("/")
    return u


def flash(request, msg, kind="success"):
    request.session.setdefault("flash", []).append([kind, msg])


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


@app.post("/login")
async def login(request: Request):
    f = await form(request)
    u = db.q1("SELECT * FROM users WHERE username=? AND active=1", (f.get("username", ""),))
    if not u or not db.check_password(f.get("password", ""), u["password_hash"]):
        return templates.TemplateResponse(request, "login.html", {"error": "Usuario o contraseña incorrectos",
                                                                  "company": db.get_setting("company")})
    request.session.clear()
    request.session["uid"] = u["id"]
    db.audit(u["username"], "login")
    if f.get("password") == "admin123":
        flash(request, "Estás usando la contraseña por defecto. Cámbiala en Mi cuenta.", "warning")
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
    elif len(f.get("new", "")) < 6 or f["new"] != f.get("confirm"):
        flash(request, "La nueva contraseña debe tener 6+ caracteres y coincidir.", "danger")
    else:
        db.ex("UPDATE users SET password_hash=? WHERE id=?", (db.hash_password(f["new"]), u["id"]))
        db.audit(u["username"], "cambio de contraseña")
        flash(request, "Contraseña actualizada.")
    return back("/cuenta")


# ------------------------------------------------------------------ dashboard
def dashboard_data():
    today = date.today()
    rows = attendance.compute(today, today)
    count = lambda st: sum(1 for r in rows if r["status"] == st)
    week = []
    for i in range(6, -1, -1):
        d = today - timedelta(days=i)
        rs = attendance.compute(d, d)
        week.append({"date": d.strftime("%d/%m"), "puntual": sum(r["status"] == "Puntual" for r in rs),
                     "tarde": sum(r["status"] == "Tarde" for r in rs), "ausente": sum(r["status"] == "Ausente" for r in rs)})
    last = db.q("""SELECT ev.*, e.name AS emp_name, d.name AS device FROM events ev
                   LEFT JOIN employees e ON e.employee_no=ev.employee_no LEFT JOIN devices d ON d.id=ev.device_id
                   ORDER BY ev.event_time DESC, ev.id DESC LIMIT 15""")
    for ev in last:
        ev["minor_name"] = MINOR_NAMES.get(ev["minor"], f"Evento {ev['minor']}") if ev["source"] == "equipo" else "Marca manual"
    devs = db.q("SELECT id, name, online, last_seen, last_error FROM devices WHERE active=1")
    return {"total": db.q1("SELECT COUNT(*) n FROM employees WHERE active=1")["n"],
            "puntual": count("Puntual"), "tarde": count("Tarde"), "ausente": count("Ausente"),
            "pendiente": count("Pendiente"), "justificado": count("Justificado"),
            "presentes": sum(1 for r in rows if r["entry"]),
            "late_list": [{"name": r["name"], "entry": r["entry"].strftime("%H:%M"), "late": r["late_min"]}
                          for r in rows if r["status"] == "Tarde"],
            "absent_list": [r["name"] for r in rows if r["status"] in ("Ausente", "Pendiente")],
            "week": week, "events": last, "devices": devs, "now": datetime.now().strftime("%H:%M:%S")}


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return render(request, "dashboard.html", user(request), d=dashboard_data())


@app.get("/api/dashboard")
def api_dashboard(request: Request):
    user(request)
    return JSONResponse(dashboard_data())


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
    raw = await request.form()
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
    upload = raw.get("photo")
    if upload is not None and getattr(upload, "filename", ""):
        try:
            jpeg = devices.prepare_face(await upload.read())
        except Exception:
            flash(request, "La foto no es una imagen válida.", "danger")
            return back(f"/empleados/{eid}" if eid else "/empleados/nuevo")
        photo_name = f"{no}_{uuid.uuid4().hex[:8]}.jpg"
        (db.PHOTOS_DIR / photo_name).write_bytes(jpeg)

    vals = [no, f["name"], f.get("document"), f.get("department_id") or None, f.get("position"), f.get("email"),
            f.get("phone"), f.get("gender") or "unknown", f.get("schedule_id") or None, f.get("hire_date") or None,
            1 if f.get("active") else 0]
    if eid:
        old = db.q1("SELECT * FROM employees WHERE id=?", (eid,))
        db.ex("""UPDATE employees SET employee_no=?, name=?, document=?, department_id=?, position=?, email=?,
                 phone=?, gender=?, schedule_id=?, hire_date=?, active=? WHERE id=?""", vals + [eid])
        if photo_name:
            db.ex("UPDATE employees SET photo=? WHERE id=?", (photo_name, eid))
            if old["photo"]:
                (db.PHOTOS_DIR / old["photo"]).unlink(missing_ok=True)
    else:
        eid = db.ex("""INSERT INTO employees(employee_no, name, document, department_id, position, email, phone,
                       gender, schedule_id, hire_date, active, photo) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    vals + [photo_name])
    db.audit(u["username"], "guardar empleado", f"{no} {f['name']}")
    emp = db.q1("SELECT * FROM employees WHERE id=?", (eid,))
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


# ------------------------------------------------------------------ departamentos / horarios / feriados
@app.get("/configuracion", response_class=HTMLResponse)
def config(request: Request):
    u = user(request, "admin", "supervisor")
    return render(request, "config.html", u, departments=db.q("SELECT * FROM departments ORDER BY name"),
                  schedules=db.q("SELECT * FROM schedules ORDER BY name"),
                  holidays=db.q("SELECT * FROM holidays ORDER BY date DESC"),
                  settings={k: db.get_setting(k) for k in ("company", "poll_seconds", "min_gap_minutes")})


@app.post("/configuracion/{what}")
async def config_save(request: Request, what: str):
    u = user(request, "admin", "supervisor")
    f = await form(request)
    if what == "departamento" and f.get("name"):
        db.ex("INSERT OR IGNORE INTO departments(name) VALUES (?)", (f["name"],))
    elif what == "departamento-eliminar":
        db.ex("UPDATE employees SET department_id=NULL WHERE department_id=?", (f["id"],))
        db.ex("DELETE FROM departments WHERE id=?", (f["id"],))
    elif what == "horario":
        days = ",".join(sorted(v for k, v in (await request.form()).multi_items() if k == "days"))
        vals = (f["name"], f["start_time"], f["end_time"], int(f.get("tolerance_min") or 0), int(f.get("lunch_min") or 0), days)
        if f.get("id"):
            db.ex("UPDATE schedules SET name=?, start_time=?, end_time=?, tolerance_min=?, lunch_min=?, days=? WHERE id=?",
                  vals + (f["id"],))
        else:
            db.ex("INSERT INTO schedules(name,start_time,end_time,tolerance_min,lunch_min,days) VALUES (?,?,?,?,?,?)", vals)
    elif what == "horario-eliminar":
        db.ex("UPDATE employees SET schedule_id=NULL WHERE schedule_id=?", (f["id"],))
        db.ex("DELETE FROM schedules WHERE id=?", (f["id"],))
    elif what == "feriado" and f.get("date"):
        db.ex("INSERT OR REPLACE INTO holidays(date, name) VALUES (?,?)", (f["date"], f.get("name")))
    elif what == "feriado-eliminar":
        db.ex("DELETE FROM holidays WHERE id=?", (f["id"],))
    elif what == "general" and u["role"] == "admin":
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
    try:
        emps, evs = await run_in_threadpool(db.restore_zip, await up.read())
    except ValueError as e:
        flash(request, str(e), "danger")
        return back("/configuracion")
    db.audit(u["username"], "respaldo restaurado", f"{up.filename}: {emps} empleados, {evs} eventos")
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
    rows = db.q(f"""SELECT ev.*, e.name AS emp_name, d.name AS device FROM events ev
                    LEFT JOIN employees e ON e.employee_no=ev.employee_no LEFT JOIN devices d ON d.id=ev.device_id
                    WHERE {' AND '.join(where)} ORDER BY ev.event_time DESC LIMIT 1000""", args)
    return render(request, "events.html", u, rows=rows, desde=d1, hasta=d2, emp=emp, dev=dev, todos=todos,
                  emps=db.q("SELECT name, employee_no FROM employees ORDER BY name"),
                  devs=db.q("SELECT id, name FROM devices ORDER BY name"))


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
    return Response(data, media_type="image/jpeg")


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
    return render(request, "devices.html", u, devs=devs)


@app.post("/dispositivos/guardar")
async def device_save(request: Request):
    u = user(request, "admin")
    f = await form(request)
    vals = [f["name"], f["host"], int(f.get("port") or 80), 1 if f.get("https") else 0, f["username"],
            devices.local_offset(), int(f.get("door_no") or 1), f.get("direction") or "ambos",
            1 if f.get("active") else 0]
    if f.get("id"):
        db.ex("""UPDATE devices SET name=?, host=?, port=?, https=?, username=?, tz_offset=?, door_no=?, direction=?,
                 active=? WHERE id=?""", vals + [f["id"]])
        if f.get("password"):
            db.ex("UPDATE devices SET password=? WHERE id=?", (f["password"], f["id"]))
        did = int(f["id"])
    else:
        did = db.ex("""INSERT INTO devices(name, host, port, https, username, tz_offset, door_no, direction, active,
                       password) VALUES (?,?,?,?,?,?,?,?,?,?)""", vals + [f.get("password", "")])
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
    u = user(request, "admin", "supervisor") if action.startswith("puerta") else user(request, "admin")
    dev = db.q1("SELECT * FROM devices WHERE id=?", (did,))
    c = devices.client(dev)
    try:
        if action == "probar":
            info = await run_in_threadpool(devices.refresh_info, dev)
            n = await run_in_threadpool(c.user_count)
            t = await run_in_threadpool(c.get_time)
            flash(request, f"En línea: {info.get('model')} (serie {info.get('serialNumber')}), {n} usuarios, "
                           f"hora del equipo {t.get('localTime', '?')}.")
        elif action.startswith("puerta-"):
            cmd = {"puerta-abrir": "open", "puerta-cerrar": "close", "puerta-siempre-abierta": "alwaysOpen",
                   "puerta-normal": "close", "puerta-siempre-cerrada": "alwaysClose"}[action]
            await run_in_threadpool(c.door, cmd, dev["door_no"] or 1)
            flash(request, f"Comando '{cmd}' enviado a la puerta de {dev['name']}.")
        elif action == "hora":
            await run_in_threadpool(c.set_time, datetime.now(), devices.local_offset())
            flash(request, f"Hora de {dev['name']} sincronizada con este servidor.")
        elif action == "eventos":
            since = datetime.now() - timedelta(days=int((await form(request)).get("dias") or 31))
            n = await run_in_threadpool(devices.pull_events, dev, since)
            flash(request, f"Descarga completa: {n} eventos nuevos.")
        elif action == "reiniciar":
            await run_in_threadpool(c.reboot)
            flash(request, f"{dev['name']} se está reiniciando.", "warning")
        elif action == "eliminar":
            db.ex("UPDATE devices SET active=0 WHERE id=?", (did,))
            flash(request, "Dispositivo desactivado (su historial se conserva).")
        db.audit(u["username"], f"dispositivo {action}", dev["name"])
    except ISAPIError as e:
        flash(request, f"{dev['name']}: {e}", "danger")
    return back(request.headers.get("referer") or "/dispositivos")


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
    if f.get("id"):
        if int(f["id"]) == u["id"] and (f.get("role") != "admin" or not f.get("active")):
            flash(request, "No puedes quitarte el rol de administrador ni desactivarte.", "danger")
            return back("/usuarios")
        db.ex("UPDATE users SET full_name=?, role=?, active=? WHERE id=?",
              (f.get("full_name"), f["role"], 1 if f.get("active") else 0, f["id"]))
        if f.get("password"):
            db.ex("UPDATE users SET password_hash=? WHERE id=?", (db.hash_password(f["password"]), f["id"]))
    else:
        if not f.get("username") or len(f.get("password", "")) < 6:
            flash(request, "Usuario y contraseña (6+ caracteres) son obligatorios.", "danger")
            return back("/usuarios")
        if db.q1("SELECT 1 FROM users WHERE username=?", (f["username"],)):
            flash(request, "Ese nombre de usuario ya existe.", "danger")
            return back("/usuarios")
        db.ex("INSERT INTO users(username, full_name, password_hash, role) VALUES (?,?,?,?)",
              (f["username"], f.get("full_name"), db.hash_password(f["password"]), f.get("role", "consulta")))
    db.audit(u["username"], "guardar usuario", f.get("username") or f.get("id"))
    flash(request, "Usuario guardado.")
    return back("/usuarios")
