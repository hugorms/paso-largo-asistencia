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


def parse_date(s, default=None):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return default


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
                    s.tolerance_min, s.lunch_min, s.days
                    FROM employees e LEFT JOIN departments d ON d.id=e.department_id
                    LEFT JOIN schedules s ON s.id=e.schedule_id
                    WHERE {' AND '.join(where)} ORDER BY e.name""", args)
    if not emps:
        return []

    devices = {d["id"]: d["direction"] for d in db.q("SELECT id, direction FROM devices")}
    marks = defaultdict(list)
    nos = [e["employee_no"] for e in emps]
    ph = ",".join("?" * len(nos))
    for ev in db.q(f"""SELECT employee_no, event_time, device_id, attendance_status FROM events
                       WHERE valid=1 AND employee_no IN ({ph}) AND event_time >= ? AND event_time < ?
                       ORDER BY event_time""",
                   nos + [start.isoformat(), (end + timedelta(days=1)).isoformat()]):
        t = datetime.fromisoformat(ev["event_time"])
        st = (ev["attendance_status"] or "").lower()
        if st in ("checkin", "breakin", "overtimein"):
            kind = "in"
        elif st in ("checkout", "breakout", "overtimeout"):
            kind = "out"
        else:
            kind = {"entrada": "in", "salida": "out"}.get(devices.get(ev["device_id"]), "any")
        marks[(ev["employee_no"], t.date())].append((t, kind))

    holidays = {h["date"]: h["name"] for h in db.q("SELECT date, name FROM holidays WHERE date BETWEEN ? AND ?",
                                                   (start.isoformat(), end.isoformat()))}
    leaves = defaultdict(list)
    for lv in db.q("SELECT * FROM leaves WHERE date_to >= ? AND date_from <= ?", (start.isoformat(), end.isoformat())):
        leaves[lv["employee_id"]].append(lv)
    min_gap = timedelta(minutes=int(db.get_setting("min_gap_minutes", "2")))
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
            ins = [t for t, k in ms if k in ("in", "any")]
            outs = [t for t, k in ms if k in ("out", "any")]
            entry = min(ins) if ins else None
            exit_ = max(outs) if outs else None
            if entry and exit_ and exit_ - entry < min_gap:
                exit_ = None
            row = {"employee_id": e["id"], "employee_no": e["employee_no"], "name": e["name"],
                   "department": e["department"] or "", "date": d, "weekday": d.isoweekday(),
                   "schedule": e["schedule_name"] or "Sin horario", "entry": entry, "exit": exit_,
                   "marks": len(ms), "late_min": 0, "early_min": 0, "overtime_min": 0,
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
            elif not ms:
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
            rows.append(row)
    return rows


def summarize(rows):
    out = {}
    for r in rows:
        s = out.setdefault(r["employee_id"], {
            "employee_id": r["employee_id"], "employee_no": r["employee_no"], "name": r["name"],
            "department": r["department"], "days_present": 0, "late_days": 0, "late_min": 0,
            "absences": 0, "justified": 0, "worked_min": 0, "overtime_min": 0, "early_min": 0,
            "missing_exit": 0})
        if r["status"] in ("Puntual", "Tarde", "Trabajó en día libre"):
            s["days_present"] += 1
        if r["status"] == "Tarde":
            s["late_days"] += 1
            s["late_min"] += r["late_min"]
        if r["status"] == "Ausente":
            s["absences"] += 1
        if r["status"] == "Justificado":
            s["justified"] += 1
        if r["note"] == "Sin marca de salida":
            s["missing_exit"] += 1
        s["worked_min"] += r["worked_min"]
        s["overtime_min"] += r["overtime_min"]
        s["early_min"] += r["early_min"]
    return sorted(out.values(), key=lambda x: x["name"])


def hm(minutes):
    minutes = int(minutes or 0)
    return f"{minutes // 60}:{minutes % 60:02d}"
