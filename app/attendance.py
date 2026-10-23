"""Cálculo de asistencia a partir de las marcaciones del equipo."""
from collections import defaultdict
from datetime import date, datetime, timedelta

from . import db

STATUS_COLORS = {
    "Puntual": "success", "Tarde": "warning", "Ausente": "danger", "Justificado": "info",
    "Feriado": "secondary", "Libre": "light", "Pendiente": "light", "Trabajó en día libre": "primary",
}


def _t(d: date, hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":")[:2])
    return datetime(d.year, d.month, d.day, h, m)


def _daterange(a: date, b: date):
    while a <= b:
        yield a
        a += timedelta(days=1)


def in_window(t: datetime, a, b) -> bool:
    """Sin ventana configurada cuenta cualquier hora."""
    if not a or not b:
        return True
    return a <= t.strftime("%H:%M") <= b


def side(t: datetime, e) -> str:
    """Si una marca suelta es de entrada o de salida: lo dicen las ventanas del horario y, fuera de
    ellas (o sin ventanas), si cae antes o después de la mitad de la jornada."""
    if e.get("in_from") and in_window(t, e["in_from"], e["in_to"]):
        return "in"
    if e.get("out_from") and in_window(t, e["out_from"], e["out_to"]):
        return "out"
    if not e.get("start_time") or not e.get("end_time"):
        return "in" if t.hour < 12 else "out"
    a, b = _t(t.date(), e["start_time"]), _t(t.date(), e["end_time"])
    return "in" if t < a + (b - a) / 2 else "out"


def parse_date(s, default=None):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return default


def pick_marks(ms, e, d: date, now: datetime, min_gap: timedelta):
    """Entrada = primera marca del día y salida = la última. Las marcas del medio no cuentan.
    ms: lista de (hora, tipo 'in'|'out'|'any', es_manual). Devuelve (entrada, salida, marcas_que_no_cuentan)."""
    ins = [t for t, k, _ in ms if k in ("in", "any")]
    outs = [t for t, k, _ in ms if k in ("out", "any")]
    entry = min(ins) if ins else None
    exit_ = max(outs) if outs else None
    if entry and exit_ and exit_ - entry < min_gap:
        # En realidad es una sola marca (o dos seguidas): las ventanas dicen si fue entrada o salida
        forced = {k for t, k, _ in ms if entry <= t <= exit_} - {"any"}
        kind = forced.pop() if len(forced) == 1 else side(entry, e)
        if kind == "in":
            exit_ = None
        else:
            entry = None
    if exit_ and not entry and side(exit_, e) == "in" and all(k == "any" for t, k, _ in ms if t == exit_):
        entry, exit_ = exit_, None
    # Hoy, antes de la hora de salida, una marca del medio (p. ej. a las 12:00) aún no es la salida
    if (exit_ and d == now.date() and e.get("end_time") and now < _t(d, e["end_time"]) and e.get("out_from")
            and not in_window(exit_, e["out_from"], e["out_to"])
            and not any(t == exit_ and (man or k == "out") for t, k, man in ms)):
        exit_ = None
    used = {entry, exit_}
    outside = sum(1 for t, k, man in ms if t not in used and not man
                  and not in_window(t, e.get("in_from"), e.get("in_to"))
                  and not in_window(t, e.get("out_from"), e.get("out_to")))
    return entry, exit_, outside


def compute(start: date, end: date, employee_id=None, department_id=None, now=None):
    """Devuelve una fila por empleado y día con entrada, salida, tardanza, horas y estado."""
    now = now or datetime.now()
    end = min(end, now.date())
    if end < start:
        return []
    where, args = ["e.active=1"], []
    if employee_id:
        where, args = ["e.id=?"], [employee_id]
    if department_id:
        where.append("e.department_id=?")
        args.append(department_id)
    emps = db.q(f"""SELECT e.*, d.name AS department, s.name AS schedule_name, s.start_time, s.end_time,
                    s.tolerance_min, s.lunch_min, s.days, s.in_from, s.in_to, s.out_from, s.out_to
                    FROM employees e LEFT JOIN departments d ON d.id=e.department_id
                    LEFT JOIN schedules s ON s.id=e.schedule_id
                    WHERE {' AND '.join(where)} ORDER BY e.name""", args)
    if not emps:
        return []

    devices = {d["id"]: d["direction"] for d in db.q("SELECT id, direction FROM devices")}
    # El tipo Entrada/Salida que manda el terminal solo se respeta si ese equipo alguna vez mandó una
    # "Salida": hay equipos que ponen "Entrada" a todas las marcas y entonces nadie tendría salida.
    trusts_status = {r["device_id"] for r in db.q(
        """SELECT DISTINCT device_id FROM events WHERE source='equipo'
           AND lower(attendance_status) IN ('checkout','breakout','overtimeout')""")}
    marks = defaultdict(list)
    nos = [e["employee_no"] for e in emps]
    ph = ",".join("?" * len(nos))
    for ev in db.q(f"""SELECT employee_no, event_time, device_id, attendance_status, source FROM events
                       WHERE valid=1 AND employee_no IN ({ph}) AND event_time >= ? AND event_time < ?
                       ORDER BY event_time""",
                   nos + [start.isoformat(), (end + timedelta(days=1)).isoformat()]):
        t = datetime.fromisoformat(ev["event_time"])
        manual = ev["source"] == "manual"
        st = (ev["attendance_status"] or "").lower() if manual or ev["device_id"] in trusts_status else ""
        if st in ("checkin", "breakin", "overtimein"):
            kind = "in"
        elif st in ("checkout", "breakout", "overtimeout"):
            kind = "out"
        else:
            kind = {"entrada": "in", "salida": "out"}.get(devices.get(ev["device_id"]), "any")
        marks[(ev["employee_no"], t.date())].append((t, kind, manual))

    holidays = {h["date"]: h["name"] for h in db.q("SELECT date, name FROM holidays WHERE date BETWEEN ? AND ?",
                                                   (start.isoformat(), end.isoformat()))}
    leaves = defaultdict(list)
    for lv in db.q("SELECT * FROM leaves WHERE date_to >= ? AND date_from <= ?", (start.isoformat(), end.isoformat())):
        leaves[lv["employee_id"]].append(lv)
    min_gap = timedelta(minutes=db.int_setting("min_gap_minutes", 2))
    # La asistencia cuenta desde que tiene rostro registrado (o desde su primera marca), y no antes de
    # su fecha de ingreso: quien aún no tiene rostro no acumula faltas.
    first_mark = {r["employee_no"]: r["d"][:10] for r in db.q(
        f"SELECT employee_no, MIN(event_time) AS d FROM events WHERE valid=1 AND employee_no IN ({ph}) GROUP BY employee_no",
        nos)}

    rows = []
    for e in emps:
        hire = min(filter(None, (parse_date(first_mark.get(e["employee_no"])), parse_date(e["face_at"]))), default=None)
        if not hire:
            continue  # sin rostro ni marcas todavía
        hire = max(hire, parse_date(e["hire_date"]) or hire)
        workdays = {int(x) for x in (e["days"] or "").split(",") if x.strip()}
        for d in _daterange(start, end):
            if hire and d < hire:
                continue
            ms = marks.get((e["employee_no"], d), [])
            entry, exit_, outside = pick_marks(ms, e, d, now, min_gap)
            row = {"employee_id": e["id"], "employee_no": e["employee_no"], "name": e["name"],
                   "department": e["department"] or "", "date": d, "weekday": d.isoweekday(),
                   "schedule": e["schedule_name"] or "Sin horario", "entry": entry, "exit": exit_,
                   "marks": len(ms), "outside": outside, "late_min": 0, "early_min": 0, "overtime_min": 0,
                   "worked_min": 0, "status": "", "note": ""}

            if entry and exit_:
                span = (exit_ - entry).total_seconds() / 60
                lunch = e["lunch_min"] or 0
                row["worked_min"] = int(span - lunch if span > 5 * 60 else span)

            leave = next((lv for lv in leaves[e["id"]] if lv["date_from"] <= d.isoformat() <= lv["date_to"]), None)
            has_schedule = bool(e["start_time"])
            is_workday = has_schedule and d.isoweekday() in workdays

            if d.isoformat() in holidays:
                row["status"], row["note"] = "Feriado", holidays[d.isoformat()]
            elif leave and not ms:
                row["status"], row["note"] = "Justificado", leave["kind"] + (f": {leave['notes']}" if leave["notes"] else "")
            elif not is_workday:
                if not ms:
                    continue  # días libres sin marcas no se listan
                row["status"] = "Trabajó en día libre" if has_schedule else "Puntual"
            elif not entry and not exit_:  # sin marcas, o solo fuera de las ventanas
                start_dt = _t(d, e["start_time"]) + timedelta(minutes=e["tolerance_min"] or 0)
                row["status"] = "Pendiente" if d == now.date() and now < _t(d, e["end_time"]) else "Ausente"
                if row["status"] == "Pendiente" and now > start_dt:
                    row["note"] = "Aún no marca"
            else:
                s_dt, e_dt = _t(d, e["start_time"]), _t(d, e["end_time"])
                if entry:
                    late = int((entry - s_dt).total_seconds() // 60)
                    row["late_min"] = late if late > (e["tolerance_min"] or 0) else 0
                if exit_ and (d < now.date() or now > e_dt):  # hoy la última marca aún no es la salida
                    row["early_min"] = max(0, int((e_dt - exit_).total_seconds() // 60))
                if exit_:
                    row["overtime_min"] = max(0, int((exit_ - e_dt).total_seconds() // 60))
                row["status"] = "Tarde" if row["late_min"] else "Puntual"
                if not exit_ and (d < now.date() or now > e_dt):
                    row["note"] = "Sin marca de salida"
                elif not entry:
                    row["note"] = "Sin marca de entrada"
                elif row["early_min"]:
                    row["note"] = f"Salida anticipada {row['early_min']} min"
            if outside and row["status"] not in ("Feriado", "Justificado"):
                extra = f"{outside} marca{'s' if outside > 1 else ''} intermedia{'s' if outside > 1 else ''} (no cuenta{'n' if outside > 1 else ''})"
                row["note"] = f"{row['note']} · {extra}" if row["note"] else extra
            rows.append(row)
    return rows


def mark_roles(start: date, end: date):
    """Qué marcas quedaron como entrada o salida de cada día: {(n° empleado, hora): 'Entrada'|'Salida'}."""
    roles = {}
    for r in compute(start, end):
        if r["entry"]:
            roles[(r["employee_no"], r["entry"])] = "Entrada"
        if r["exit"]:
            roles[(r["employee_no"], r["exit"])] = "Salida"
    return roles


def summarize(rows):
    out = {}
    for r in rows:
        s = out.setdefault(r["employee_id"], {
            "employee_id": r["employee_id"], "employee_no": r["employee_no"], "name": r["name"],
            "department": r["department"], "days_present": 0, "late_days": 0, "late_min": 0,
            "absences": 0, "justified": 0, "worked_min": 0, "overtime_min": 0, "early_min": 0,
            "missing_exit": 0})
        if r["status"] in ("Puntual", "Tarde", "Trabajó en día libre") or (r["status"] == "Feriado" and r["entry"]):
            s["days_present"] += 1
        if r["status"] == "Tarde":
            s["late_days"] += 1
            s["late_min"] += r["late_min"]
        if r["status"] == "Ausente":
            s["absences"] += 1
        if r["status"] == "Justificado":
            s["justified"] += 1
        if r["note"].startswith("Sin marca de salida"):
            s["missing_exit"] += 1
        s["worked_min"] += r["worked_min"]
        s["overtime_min"] += r["overtime_min"]
        s["early_min"] += r["early_min"]
    return sorted(out.values(), key=lambda x: x["name"])


def hm(minutes):
    minutes = int(minutes or 0)
    return f"{minutes // 60}:{minutes % 60:02d}"
