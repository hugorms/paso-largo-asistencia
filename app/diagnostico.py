"""Diagnóstico del biométrico: prueba, con la clave guardada, cada consulta que hace el lector y muestra
cuál responde y cuál falla. Solo lee (no cambia nada en el equipo ni en la base de datos).

Uso en el servidor:  docker exec paso-largo python3 -m app.diagnostico
"""
from datetime import datetime, timedelta

from . import db, devices


def main():
    tz = devices.local_offset()
    hoy = datetime.now()
    desde = (hoy - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%S") + tz
    hasta = (hoy + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S") + tz
    for dv in db.q("SELECT * FROM devices WHERE active=1"):
        print(f"\n== {dv['name']}  {dv['host']}:{dv['port']}  usuario={dv['username']}")
        c = devices.client(dv)
        pasos = [
            ("datos del equipo", lambda: c.device_info().get("model")),
            ("hora del equipo", lambda: c.get_time().get("localTime")),
            ("usuarios (rostros)", lambda: f"{len(c.search_users())} usuarios"),
            ("marcas ultimas 6 h", lambda: f"{len(c.search_events(desde, hasta))} marcas"),
        ]
        for nombre, f in pasos:
            try:
                print(f"  OK     {nombre}: {f()}")
            except Exception as e:
                print(f"  FALLA  {nombre}: {e}")
                if "401" in str(e) and nombre == "datos del equipo":
                    break  # sin acceso: no se insiste (cada intento fallido acerca el bloqueo)
        ev = db.q1("SELECT picture_url FROM events WHERE device_id=? AND picture_url IS NOT NULL "
                   "ORDER BY event_time DESC LIMIT 1", (dv["id"],))
        if ev:
            try:
                print(f"  OK     foto de marca: {len(c.fetch_picture(ev['picture_url']))} bytes")
            except Exception as e:
                print(f"  FALLA  foto de marca: {e}")
        else:
            print("  --     foto de marca: ninguna marca guardada tiene foto")


if __name__ == "__main__":
    main()
