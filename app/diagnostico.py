"""Diagnóstico del biométrico: prueba, con la clave guardada, cada consulta que hace el lector y dice
cuál responde y cuál falla. Solo lee (no cambia nada en el equipo ni en la base de datos).

En la app: botón «Probar» de cada equipo.   En el servidor: docker exec paso-largo python3 -m app.diagnostico
"""
from datetime import datetime, timedelta

from . import db, devices


def run(dv):
    """Devuelve una lista de (ok, texto) con el resultado de cada consulta."""
    tz = devices.local_offset()
    hoy = datetime.now()
    desde = (hoy - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%S") + tz
    hasta = (hoy + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S") + tz
    c = devices.client(dv)
    out = [(True, f"usuario del equipo guardado: «{dv['username']}»")]
    pasos = [
        ("datos del equipo", lambda: c.device_info().get("model")),
        ("hora del equipo", lambda: (c.get_time().get("localTime") or "")[:19].replace("T", " ")),
        ("usuarios y rostros", lambda: f"{len(c.search_users())} usuarios"),
        ("marcas de las últimas 6 h", lambda: f"{len(c.search_events(desde, hasta))} marcas"),
    ]
    for nombre, f in pasos:
        try:
            out.append((True, f"{nombre}: {f()}"))
        except Exception as e:
            out.append((False, f"{nombre}: {e}"))
            if nombre == "datos del equipo" and "401" in str(e):
                return out  # sin acceso: no se insiste (cada intento fallido acerca el bloqueo)
    ev = db.q1("SELECT picture_url FROM events WHERE device_id=? AND picture_url IS NOT NULL "
               "ORDER BY event_time DESC LIMIT 1", (dv["id"],))
    if ev:
        try:
            out.append((True, f"foto de marca: {len(c.fetch_picture(ev['picture_url']))} bytes"))
        except Exception as e:
            out.append((False, f"foto de marca: {e}"))
    else:
        out.append((True, "foto de marca: ninguna marca guardada tiene foto"))
    return out


def main():
    for dv in db.q("SELECT * FROM devices WHERE active=1"):
        print(f"\n== {dv['name']}  {dv['host']}:{dv['port']}")
        for ok, txt in run(dv):
            print(f"  {'OK   ' if ok else 'FALLA'}  {txt}")


if __name__ == "__main__":
    main()
