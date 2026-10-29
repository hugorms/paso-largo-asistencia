"""Protecciones para cuando el sistema se abre a internet (túnel de Cloudflare) sin romper el uso en la red local."""
import hashlib
import logging
import os
import threading
import time
from urllib.parse import urlsplit

from starlette.responses import PlainTextResponse

log = logging.getLogger("seguridad")

# Intentos fallidos de ingreso: por IP y por usuario, en una ventana de 15 minutos
WINDOW = 15 * 60
MAX_PER_IP = 5
MAX_PER_USER = 10
_fails: dict[str, list[float]] = {}
_lock = threading.Lock()

# Tamaño máximo de lo que se puede subir (fotos, Excel, respaldos)
MAX_BODY = 100 * 1024 * 1024

# Otros orígenes permitidos además del propio (separados por coma), por si un proxy cambia el Host
EXTRA_ORIGINS = {o.strip().lower() for o in os.environ.get("ORIGENES_PERMITIDOS", "").split(",") if o.strip()}

CSP = ("default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
       "script-src 'self' 'unsafe-inline'; connect-src 'self'; font-src 'self'; object-src 'none'; "
       "base-uri 'self'; form-action 'self'; frame-ancestors 'none'")


def client_ip(request):
    # Por el túnel la IP real del visitante viene en CF-Connecting-IP
    return request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")


def _recent(key, now):
    hits = [t for t in _fails.get(key, []) if now - t < WINDOW]
    if hits:
        _fails[key] = hits
    else:
        _fails.pop(key, None)
    return hits


def login_blocked(ip, username):
    """Minutos que faltan para poder intentar de nuevo (0 = puede intentar)."""
    now = time.time()
    with _lock:
        waits = [WINDOW - (now - hits[0]) for key, limit in ((f"ip:{ip}", MAX_PER_IP), (f"u:{username.lower()}", MAX_PER_USER))
                 if len(hits := _recent(key, now)) >= limit]
    return int(max(waits) // 60) + 1 if waits else 0


def login_failed(ip, username):
    now = time.time()
    with _lock:
        for key in (f"ip:{ip}", f"u:{username.lower()}"):
            _fails.setdefault(key, []).append(now)
        if len(_fails) > 5000:  # que nadie llene la memoria probando miles de usuarios
            for key in list(_fails):
                _recent(key, now)
    log.warning("Ingreso fallido: usuario %r desde %s", username, ip)


def login_ok(ip, username):
    with _lock:
        _fails.pop(f"ip:{ip}", None)
        _fails.pop(f"u:{username.lower()}", None)


def password_tag(secret, password_hash):
    """Huella de la contraseña guardada en la sesión: si la contraseña cambia, las sesiones viejas se cierran.
    No se guarda el hash (la cookie de sesión se puede leer, solo está firmada)."""
    return hashlib.sha256(f"{secret}:{password_hash}".encode()).hexdigest()[:16]


def safe_back(referer, default):
    """Vuelve a la página anterior, pero solo dentro de este sistema (nunca a otro sitio)."""
    # Los navegadores leen «/\\» como «//» (otro sitio): se normaliza antes de revisar
    parts = urlsplit((referer or "").replace("\\", "/"))
    path = parts.path if parts.path.startswith("/") and not parts.path.startswith("//") else ""
    return (path + (f"?{parts.query}" if parts.query else "")) if path else default


def _host_of(url):
    return urlsplit(url).netloc.lower() if url and url != "null" else None


def _same_origin(request):
    """Los formularios solo se aceptan si vienen de las páginas de este mismo sistema (protección CSRF)."""
    source = _host_of(request.headers.get("origin")) or _host_of(request.headers.get("referer"))
    if request.headers.get("origin") == "null":
        return False
    if source is None:  # sin Origin ni Referer: no es un navegador enviando la cookie de otro sitio
        return True
    own = {h.lower() for h in (request.headers.get("host"), request.headers.get("x-forwarded-host")) if h}
    return source in own or source in EXTRA_ORIGINS


async def middleware(request, call_next):
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        if not _same_origin(request):
            log.warning("Formulario rechazado (otro origen): origin=%s referer=%s host=%s",
                        request.headers.get("origin"), request.headers.get("referer"), request.headers.get("host"))
            return PlainTextResponse("Solicitud rechazada: vuelve a abrir la página e inténtalo de nuevo.", 403)
        size = request.headers.get("content-length")
        if size and size.isdigit() and int(size) > MAX_BODY:
            return PlainTextResponse("El archivo es demasiado grande.", 413)
    response = await call_next(request)
    h = response.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("X-Frame-Options", "DENY")
    h.setdefault("Referrer-Policy", "same-origin")
    h.setdefault("Permissions-Policy", "camera=(self), microphone=(), geolocation=()")
    h.setdefault("Content-Security-Policy", CSP)
    if not request.url.path.startswith("/static/"):
        # Datos de empleados: que no queden guardados en cachés intermedias ni en el navegador
        h.setdefault("Cache-Control", "no-store")
    if request.url.scheme == "https":
        h.setdefault("Strict-Transport-Security", "max-age=31536000")
        # Por HTTPS (túnel) la cookie de sesión viaja solo cifrada; en la red local (http) sigue igual
        cookies = [(k, v) for k, v in h.raw if k == b"set-cookie"]
        if cookies:
            others = [(k, v) for k, v in h.raw if k != b"set-cookie"]
            h.raw[:] = others + [(k, v if b"; secure" in v.lower() else v + b"; Secure") for k, v in cookies]
    return response
