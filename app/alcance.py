"""Qué locales puede ver cada usuario.

El admin ve todos los locales; el encargado y el de consulta solo los locales que el admin le asignó.
Quien ve más de un local puede elegir uno arriba en el menú (se guarda en la sesión); sin elegir, ve todos los suyos.
Todas las consultas a datos de un local deben pasar por aquí, en el servidor: no basta con esconder botones.
"""
from . import db

ROLES = {"admin": "Administrador", "encargado": "Encargado de local", "consulta": "Consulta"}
EDIT_ROLES = ("admin", "encargado")


def is_admin(u) -> bool:
    return bool(u) and u["role"] == "admin"


def allowed(u) -> list[int]:
    """Locales que el usuario tiene permitidos (activos)."""
    if is_admin(u):
        return [r["id"] for r in db.q("SELECT id FROM locales WHERE active=1 ORDER BY name")]
    return [r["local_id"] for r in db.q("""SELECT ul.local_id FROM user_locales ul JOIN locales l ON l.id=ul.local_id
                                           WHERE ul.user_id=? AND l.active=1 ORDER BY l.name""", (u["id"],))]


def choices(u) -> list[dict]:
    """Locales para el selector del menú."""
    ids = allowed(u)
    if not ids:
        return []
    return db.q(f"SELECT id, name FROM locales WHERE id IN ({','.join('?' * len(ids))}) ORDER BY name", ids)


def ids(request, u) -> list[int]:
    """Locales que se están viendo ahora: el elegido en el menú, o todos los permitidos."""
    ok = allowed(u)
    chosen = request.session.get("local")
    if chosen in ok:
        return [chosen]
    return ok


def current(request, u):
    """El local elegido (dict) o None si se están viendo todos los permitidos."""
    ok = allowed(u)
    chosen = request.session.get("local")
    if chosen in ok:
        return db.q1("SELECT id, name FROM locales WHERE id=?", (chosen,))
    if len(ok) == 1:
        return db.q1("SELECT id, name FROM locales WHERE id=?", (ok[0],))
    return None


def choose(request, u, local_id):
    """Cambia el local que se está viendo. local_id vacío = todos."""
    try:
        lid = int(local_id) if local_id else None
    except (TypeError, ValueError):
        lid = None
    if lid is not None and lid not in allowed(u):
        lid = None
    request.session["local"] = lid


def sql(request, u, col: str, include_null=False) -> tuple[str, list]:
    """Condición SQL «col pertenece a los locales que se ven ahora».
    include_null: el admin viendo todos también ve lo que todavía no tiene local."""
    vis = ids(request, u)
    if not vis:
        return "0=1", []
    cond = f"{col} IN ({','.join('?' * len(vis))})"
    if include_null and is_admin(u) and request.session.get("local") not in vis:
        cond = f"({cond} OR {col} IS NULL)"
    return cond, list(vis)


def can_see(request, u, local_id) -> bool:
    """¿Puede este usuario ver un registro de ese local? (lo sin local solo lo ve el admin)"""
    if local_id is None:
        return is_admin(u)
    return local_id in allowed(u)


def default_local(request, u):
    """Local donde se crea algo nuevo: el elegido, o el único que tiene, o None si hay que preguntar."""
    vis = ids(request, u)
    return vis[0] if len(vis) == 1 else None


def row(request, u, table: str, rid):
    """Trae una fila de una tabla con local_id solo si el usuario puede verla; si no, None."""
    if table not in db.LOCAL_TABLES:
        raise ValueError(table)
    r = db.q1(f"SELECT * FROM {table} WHERE id=?", (rid,))
    return r if r and can_see(request, u, r["local_id"]) else None


def employee_by_no(request, u, employee_no):
    r = db.q1("SELECT * FROM employees WHERE employee_no=?", (employee_no,))
    return r if r and can_see(request, u, r["local_id"]) else None
