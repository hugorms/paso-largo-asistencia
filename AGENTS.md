# Guía del proyecto para asistentes de código (Cursor, etc.)

Sistema web de control de asistencia para terminales faciales Hikvision vía ISAPI. Python 3.10+, FastAPI, Jinja2, SQLite. Interfaz en español.

## Estructura
- `iniciar.py` / `iniciar.bat`: arranque (uvicorn `app.main:app`, puerto 8000).
- `app/isapi.py`: cliente ISAPI con Digest auth (deviceInfo, UserInfo Search/Record/Modify/Delete, FDLib FDSetUp/FaceDataRecord, AcsEvent, RemoteControl/door, time, reboot).
- `app/devices.py`: lectura de eventos (hilo `Poller` cada N s), sincronización de empleados y rostros, normalización de fotos (<200 KB). Si un equipo deja de responder, `relocate` lo busca por número de serie y le actualiza la IP.
- `app/discovery.py`: búsqueda de equipos en la red (SADP UDP 37020 + barrido /24 de ISAPI).
- `app/attendance.py`: cálculo diario (entrada/salida, tardanza, faltas, horas, extras) y resumen.
- `app/reports.py`: exportación Excel (openpyxl) y PDF (reportlab).
- `app/db.py`: esquema SQLite y helpers `q`, `q1`, `ex`, `audit`. Datos en `datos/` (variable `ASISTENCIA_DATA`).
- `app/main.py`: rutas web, sesiones y roles (admin, supervisor, consulta).
- `app/templates/`: Jinja2 + Bootstrap 5 local (`app/static/`, sin CDN).
- `simulador.py`: terminal ISAPI falso en 127.0.0.1:8090 (admin/12345) para probar sin hardware; responde a SADP. `--ip 0.0.0.0 --puerto N` para probar cambios de IP.

## Convenciones
- SQL directo con `db.q/q1/ex`, sin ORM. Las llamadas al equipo van en `run_in_threadpool`.
- Textos de UI en español. Cada acción relevante se registra con `db.audit`.
- Probar: `python simulador.py` y en otra terminal `python iniciar.py`; agregar el dispositivo 127.0.0.1:8090.
