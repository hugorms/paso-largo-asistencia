"""Arranca el sistema de asistencia.  Uso: python iniciar.py  [--puerto 8000]"""
import argparse
import socket
from pathlib import Path

import uvicorn

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--puerto", type=int, default=8000)
    p.add_argument("--host", default="0.0.0.0", help="0.0.0.0 = accesible desde otras PCs de la red")
    a = p.parse_args()
    try:
        ip = socket.gethostbyname(socket.gethostname())
    except OSError:
        ip = "127.0.0.1"
    print(f"\n  Paso Largo listo:  http://localhost:{a.puerto}   (en la red: http://{ip}:{a.puerto})")
    print("  Usuario inicial: admin / admin123  (cámbiala al entrar)\n")
    # reload: si se actualizan archivos de app/ el programa se recarga solo
    uvicorn.run("app.main:app", host=a.host, port=a.puerto, log_level="warning",
                reload=True, reload_dirs=[str(Path(__file__).parent / "app")])
