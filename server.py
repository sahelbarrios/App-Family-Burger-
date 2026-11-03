"""
DOGGIPOS MULTISEDE & AUDITORÍA (FAST-FOOD) - VERSIÓN PRODUCCIÓN UNIFICADA
Monolito Flask + PostgreSQL (Supabase Cloud) / SQLite Fallback + SPA Completa
"""
import os
import re
import sys
import json
import time
import sqlite3
import secrets
import threading
import datetime
import requests
from functools import wraps
from flask import Flask, request, jsonify, render_template_string, session, g
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)

# La clave que firma las sesiones DEBE venir del entorno (render.yaml ya la genera).
_secret = os.environ.get("SECRET_KEY", "").strip()
if not _secret:
    _secret = secrets.token_hex(32)
    print(">> [SEGURIDAD] SECRET_KEY no definida: se usa una clave temporal (las sesiones se cierran al reiniciar).")
app.secret_key = _secret
app.config.update(
    JSON_SORT_KEYS=False,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER")),
    PERMANENT_SESSION_LIFETIME=datetime.timedelta(hours=12),
)

# Detección de Base de Datos en la Nube (Supabase PostgreSQL / Render Postgres)
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_POSTGRES = False

if DATABASE_URL:
    try:
        import psycopg2
        import psycopg2.extras
        USE_POSTGRES = True
        print(">> [PRODUCCIÓN] Conectado a PostgreSQL en la nube (Supabase/Render).")
    except Exception as e:
        print(f">> [WARN] No se pudo cargar psycopg2 ({e}). Usando SQLite.")

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "burgeradmin_local.db")

class DBWrapper:
    """Adaptador universal compatible con PostgreSQL (Supabase Cloud) y SQLite."""
    def __init__(self):
        self.is_pg = USE_POSTGRES
        if self.is_pg:
            conn_str = DATABASE_URL
            if "sslmode" not in conn_str:
                conn_str += ("&" if "?" in conn_str else "?") + "sslmode=require"
            self.conn = psycopg2.connect(conn_str)
        else:
            self.conn = sqlite3.connect(DB_PATH)
            self.conn.row_factory = sqlite3.Row

    def _adapt_sql(self, sql):
        if self.is_pg:
            sql = sql.replace("?", "%s")
            sql = sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
            sql = sql.replace("DATETIME('now')", "NOW()")
            sql = sql.replace("activa = 1", "activa = TRUE")
            sql = sql.replace("activo = 1", "activo = TRUE")
            sql = sql.replace("es_almacen = 1", "es_almacen = TRUE")
            sql = sql.replace("es_almacen = 0", "es_almacen = FALSE")
            sql = sql.replace("usar_tasa_manual INTEGER NOT NULL DEFAULT 0", "usar_tasa_manual BOOLEAN NOT NULL DEFAULT FALSE")
            sql = sql.replace("es_almacen INTEGER NOT NULL DEFAULT 0", "es_almacen BOOLEAN NOT NULL DEFAULT FALSE")
            sql = sql.replace("activa INTEGER NOT NULL DEFAULT 1", "activa BOOLEAN NOT NULL DEFAULT TRUE")
            sql = sql.replace("activo INTEGER NOT NULL DEFAULT 1", "activo BOOLEAN NOT NULL DEFAULT TRUE")
        return sql

    def execute(self, sql, params=()):
        sql_adapted = self._adapt_sql(sql)
        if self.is_pg:
            cur = self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(sql_adapted, params)
            return cur
        else:
            cur = self.conn.cursor()
            cur.execute(sql_adapted, params)
            return cur

    def insert_returning_id(self, sql, params=()):
        if self.is_pg:
            sql_adapted = self._adapt_sql(sql) + " RETURNING id"
            cur = self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(sql_adapted, params)
            row = cur.fetchone()
            return row["id"] if row else None
        else:
            cur = self.conn.cursor()
            cur.execute(sql, params)
            return cur.lastrowid

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.close()

def get_db():
    return DBWrapper()

MSG_PIN = "El PIN debe tener entre 6 y 12 dígitos (solo números)."
KARDEX_ACEPTA_AJUSTE = not USE_POSTGRES


def pin_valido(pin):
    return bool(re.fullmatch(r"\d{6,12}", str(pin or "")))


def _pin_inicial(env_name):
    pin = os.environ.get(env_name, "").strip()
    if not pin_valido(pin):
        pin = "".join(secrets.choice("0123456789") for _ in range(6))
        print(f">> [SEGURIDAD] PIN inicial generado para {env_name}: {pin}  (cámbialo apenas entres)")
    return pin


def init_db():
    db = get_db()
    tablas = [
        """CREATE TABLE IF NOT EXISTS configuracion (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tasa_bcv REAL NOT NULL DEFAULT 45.50,
            tasa_manual REAL NOT NULL DEFAULT 46.00,
            tasa_reposicion REAL NOT NULL DEFAULT 47.50,
            usar_tasa_manual INTEGER NOT NULL DEFAULT 0,
            pct_reserva_operativa REAL NOT NULL DEFAULT 10.0,
            gastos_operativos_fijos REAL NOT NULL DEFAULT 0.0,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS sedes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL,
            es_almacen INTEGER NOT NULL DEFAULT 0,
            activa INTEGER NOT NULL DEFAULT 1
        )""",
        """CREATE TABLE IF NOT EXISTS usuarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL,
            username TEXT UNIQUE NOT NULL,
            pin_plain TEXT NOT NULL DEFAULT '',
            pin_hash TEXT NOT NULL,
            rol TEXT NOT NULL,
            sede_id INTEGER,
            activo INTEGER NOT NULL DEFAULT 1
        )""",
        """CREATE TABLE IF NOT EXISTS insumos_maestro (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL,
            unidad TEXT NOT NULL DEFAULT 'und',
            costo_paquete_usd REAL NOT NULL,
            rendimiento_paquete REAL NOT NULL,
            costo_unitario_usd REAL NOT NULL,
            alerta_stock_minimo REAL NOT NULL DEFAULT 10
        )""",
        """CREATE TABLE IF NOT EXISTS inventario_por_sede (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sede_id INTEGER NOT NULL,
            insumo_id INTEGER NOT NULL,
            stock_actual REAL NOT NULL DEFAULT 0,
            UNIQUE(sede_id, insumo_id)
        )""",
        """CREATE TABLE IF NOT EXISTS movimientos_kardex (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sede_id INTEGER NOT NULL,
            insumo_id INTEGER NOT NULL,
            usuario_id INTEGER,
            tipo TEXT NOT NULL,
            cantidad REAL NOT NULL,
            costo_unitario_momento REAL NOT NULL,
            motivo TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS catalogo_productos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL,
            tipo TEXT NOT NULL,
            precio_venta_usd REAL NOT NULL,
            activo INTEGER NOT NULL DEFAULT 1
        )""",
        """CREATE TABLE IF NOT EXISTS recetas_bom (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            producto_id INTEGER NOT NULL,
            insumo_id INTEGER NOT NULL,
            cantidad_consumida REAL NOT NULL,
            UNIQUE(producto_id, insumo_id)
        )""",
        """CREATE TABLE IF NOT EXISTS metodos_pago_qr (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sede_id INTEGER,
            titulo TEXT NOT NULL,
            banco TEXT NOT NULL,
            telefono TEXT NOT NULL,
            rif_cedula TEXT NOT NULL,
            qr_base64 TEXT,
            activo INTEGER NOT NULL DEFAULT 1
        )""",
        """CREATE TABLE IF NOT EXISTS turnos_caja (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sede_id INTEGER NOT NULL,
            cajero_id INTEGER NOT NULL,
            apertura_at TEXT DEFAULT CURRENT_TIMESTAMP,
            cierre_at TEXT,
            fondo_inicial_usd REAL NOT NULL DEFAULT 0,
            efectivo_declarado_cierre_usd REAL DEFAULT NULL,
            diferencia_arqueo_usd REAL DEFAULT NULL,
            estado TEXT NOT NULL DEFAULT 'ABIERTO'
        )""",
        """CREATE TABLE IF NOT EXISTS ordenes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            turno_id INTEGER,
            sede_id INTEGER NOT NULL,
            cajero_id INTEGER NOT NULL,
            cliente_nombre TEXT,
            cliente_telefono TEXT,
            total_venta_usd REAL NOT NULL,
            costo_insumos_usd REAL NOT NULL DEFAULT 0,
            tasa_aplicada REAL NOT NULL,
            tasa_reposicion_momento REAL NOT NULL,
            recibido_usd REAL NOT NULL DEFAULT 0,
            recibido_bs REAL NOT NULL DEFAULT 0,
            vuelto_usd REAL NOT NULL DEFAULT 0,
            vuelto_bs REAL NOT NULL DEFAULT 0,
            metodo_qr_id INTEGER,
            referencia_pago TEXT,
            estado_pago TEXT NOT NULL DEFAULT 'PAGADA',
            estado_cocina TEXT NOT NULL DEFAULT 'PREPARANDO',
            notas_cocina TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS orden_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            orden_id INTEGER NOT NULL,
            producto_id INTEGER NOT NULL,
            cantidad INTEGER NOT NULL,
            precio_unitario_usd REAL NOT NULL,
            costo_unitario_usd REAL NOT NULL,
            modificadores_json TEXT DEFAULT '[]'
        )""",
        """CREATE TABLE IF NOT EXISTS auditoria_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            superusuario_id INTEGER,
            sede_id INTEGER,
            accion TEXT NOT NULL,
            entidad_afectada TEXT NOT NULL,
            registro_id INTEGER,
            snapshot_anterior_json TEXT,
            snapshot_nuevo_json TEXT,
            justificacion TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )"""
    ]
    for t in tablas:
        db.execute(t)

    # Semillas iniciales
    row_cfg = db.execute("SELECT COUNT(*) as c FROM configuracion").fetchone()
    if (row_cfg["c"] if isinstance(row_cfg, dict) else row_cfg[0]) == 0:
        db.execute("INSERT INTO configuracion (tasa_bcv, tasa_manual, tasa_reposicion, usar_tasa_manual, pct_reserva_operativa, gastos_operativos_fijos) VALUES (?, ?, ?, ?, ?, ?)", (45.50, 46.00, 47.50, False if USE_POSTGRES else 0, 10.0, 0.0))

    row_sedes = db.execute("SELECT COUNT(*) as c FROM sedes").fetchone()
    if (row_sedes["c"] if isinstance(row_sedes, dict) else row_sedes[0]) == 0:
        db.execute("INSERT INTO sedes (nombre, es_almacen, activa) VALUES (?, ?, ?)", ('Almacén Central', True if USE_POSTGRES else 1, True if USE_POSTGRES else 1))
        db.execute("INSERT INTO sedes (nombre, es_almacen, activa) VALUES (?, ?, ?)", ('Sede 1 (Puesto Principal)', False if USE_POSTGRES else 0, True if USE_POSTGRES else 1))
        db.execute("INSERT INTO sedes (nombre, es_almacen, activa) VALUES (?, ?, ?)", ('Sede 2 (Puesto Norte)', False if USE_POSTGRES else 0, True if USE_POSTGRES else 1))

    row_users = db.execute("SELECT COUNT(*) as c FROM usuarios").fetchone()
    if (row_users["c"] if isinstance(row_users, dict) else row_users[0]) == 0:
        for nombre, username, env_name, rol, sede in [
            ('Propietario (Superusuario)', 'superadmin', 'ADMIN_INITIAL_PIN', 'superusuario', None),
            ('Carlos Cajero', 'cajero1', 'CAJERO1_INITIAL_PIN', 'cajero', 2),
            ('Ana Cajera', 'cajero2', 'CAJERO2_INITIAL_PIN', 'cajero', 3),
        ]:
            pin = _pin_inicial(env_name)
            db.execute("INSERT INTO usuarios (nombre, username, pin_plain, pin_hash, rol, sede_id, activo) VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (nombre, username, '', generate_password_hash(pin), rol, sede, True if USE_POSTGRES else 1))

    # Migración de seguridad: ningún PIN queda guardado en texto plano.
    for u in [dict(r) for r in db.execute("SELECT id, pin_plain, pin_hash FROM usuarios").fetchall()]:
        plano = (u.get("pin_plain") or "").strip()
        if plano:
            if not check_password_hash(u["pin_hash"], plano):
                db.execute("UPDATE usuarios SET pin_hash = ? WHERE id = ?", (generate_password_hash(plano), u["id"]))
            db.execute("UPDATE usuarios SET pin_plain = '' WHERE id = ?", (u["id"],))
    db.commit()

    # El kardex debe aceptar el tipo AJUSTE (conteo físico).
    global KARDEX_ACEPTA_AJUSTE
    if USE_POSTGRES:
        try:
            db.execute("ALTER TABLE movimientos_kardex DROP CONSTRAINT IF EXISTS movimientos_kardex_tipo_check")
            db.execute("ALTER TABLE movimientos_kardex ADD CONSTRAINT movimientos_kardex_tipo_check CHECK (tipo IN ('COMPRA','VENTA','TRANSFERENCIA','MERMA','CONSUMO','REVERSION_AUDITORIA','AJUSTE'))")
            db.commit()
            KARDEX_ACEPTA_AJUSTE = True
        except Exception as e:
            db.conn.rollback()
            print(f">> [WARN] No se pudo ampliar el constraint del kardex ({e}). Los ajustes se registrarán como COMPRA/MERMA.")

    row_ins = db.execute("SELECT COUNT(*) as c FROM insumos_maestro").fetchone()
    if (row_ins["c"] if isinstance(row_ins, dict) else row_ins[0]) == 0:
        insumos_seed = [
            ('Pan Pequeño Estándar', 'und', 1.98, 10, 0.1980, 10),
            ('Pan Yumpi Grande', 'und', 2.30, 10, 0.2300, 5),
            ('Salchicha Recado', 'und', 3.00, 10, 0.3000, 15),
            ('Salsa de Tomate', 'porcion', 5.00, 250, 0.0200, 30),
            ('Mayonesa', 'porcion', 5.00, 100, 0.0500, 25),
            ('Mostaza', 'porcion', 3.00, 300, 0.0100, 30),
            ('Base para Salsa', 'porcion', 6.00, 150, 0.0400, 20),
            ('Verduras (Cebolla, Repollo, Cilantro)', 'porcion', 4.00, 60, 0.0667, 15),
            ('Papitas Ralladas', 'porcion', 3.76, 100, 0.0376, 25),
            ('Papel Envoltorio / Servilleta', 'und', 2.00, 100, 0.0200, 25),
            ('Refresco Glu 400ml', 'und', 5.90, 15, 0.3933, 5),
            ('Refresco 1 Litro', 'und', 5.00, 6, 0.8333, 3),
        ]
        for ins in insumos_seed:
            db.execute("INSERT INTO insumos_maestro (nombre, unidad, costo_paquete_usd, rendimiento_paquete, costo_unitario_usd, alerta_stock_minimo) VALUES (?, ?, ?, ?, ?, ?)", ins)

    row_prods = db.execute("SELECT COUNT(*) as c FROM catalogo_productos").fetchone()
    if (row_prods["c"] if isinstance(row_prods, dict) else row_prods[0]) == 0:
        prods_seed = [
            ('Perro Pequeño Clásico', 'INDIVIDUAL', 1.50, 1),
            ('Perro Grande Yumpi Especial', 'INDIVIDUAL', 2.00, 1),
            ('Combo Dúo Clásico (2 Peq + 1 Glu)', 'COMBO', 3.00, 1),
            ('Combo Yumpi Individual (1 Yumpi + 1 Glu)', 'COMBO', 2.50, 1),
            ('Combo Yumpi Doble (2 Yumpi + 1 Glu)', 'COMBO', 4.00, 1),
            ('Combo Familiar (4 Peq + Refresco 1L)', 'COMBO', 6.00, 1),
            ('Refresco Glu 400ml', 'BEBIDA', 1.00, 1),
            ('Refresco 1 Litro', 'BEBIDA', 1.75, 1),
            ('Extra Salchicha', 'EXTRA', 0.60, 1),
            ('Extra Papitas / Queso', 'EXTRA', 0.50, 1),
        ]
        for p in prods_seed:
            db.execute("INSERT INTO catalogo_productos (nombre, tipo, precio_venta_usd, activo) VALUES (?, ?, ?, ?)", (p[0], p[1], p[2], True if USE_POSTGRES else 1))

        recetas_seed = [
            (1, 1, 1), (1, 3, 1), (1, 4, 1), (1, 5, 1), (1, 6, 1), (1, 7, 1), (1, 8, 1), (1, 9, 1), (1, 10, 1),
            (2, 2, 1), (2, 3, 1), (2, 4, 1), (2, 5, 1), (2, 6, 1), (2, 7, 1), (2, 8, 1), (2, 9, 1), (2, 10, 1),
            (3, 1, 2), (3, 3, 2), (3, 4, 2), (3, 5, 2), (3, 6, 2), (3, 7, 2), (3, 8, 2), (3, 9, 2), (3, 10, 2), (3, 11, 1),
            (4, 2, 1), (4, 3, 1), (4, 4, 1), (4, 5, 1), (4, 6, 1), (4, 7, 1), (4, 8, 1), (4, 9, 1), (4, 10, 1), (4, 11, 1),
            (5, 2, 2), (5, 3, 2), (5, 4, 2), (5, 5, 2), (5, 6, 2), (5, 7, 2), (5, 8, 2), (5, 9, 2), (5, 10, 2), (5, 11, 1),
            (6, 1, 4), (6, 3, 4), (6, 4, 4), (6, 5, 4), (6, 6, 4), (6, 7, 4), (6, 8, 4), (6, 9, 4), (6, 10, 4), (6, 12, 1),
            (7, 11, 1), (8, 12, 1), (9, 3, 1), (10, 9, 1)
        ]
        for r in recetas_seed:
            db.execute("INSERT INTO recetas_bom (producto_id, insumo_id, cantidad_consumida) VALUES (?, ?, ?)", r)

    row_inv = db.execute("SELECT COUNT(*) as c FROM inventario_por_sede").fetchone()
    if (row_inv["c"] if isinstance(row_inv, dict) else row_inv[0]) == 0:
        # Stock exacto de tu primera tanda de 30 perros (20 peq, 10 Yumpi, 30 salchichas, 15 Glu)
        stock_inicial_sede = {1: 20, 2: 10, 3: 30, 4: 250, 5: 100, 6: 300, 7: 150, 8: 60, 9: 100, 10: 100, 11: 15, 12: 6}
        for s_id in [2, 3]:
            for i_id, st in stock_inicial_sede.items():
                db.execute("INSERT INTO inventario_por_sede (sede_id, insumo_id, stock_actual) VALUES (?, ?, ?)", (s_id, i_id, st))
        for i_id, st in stock_inicial_sede.items():
            db.execute("INSERT INTO inventario_por_sede (sede_id, insumo_id, stock_actual) VALUES (?, ?, ?)", (1, i_id, st * 5))

    row_qr = db.execute("SELECT COUNT(*) as c FROM metodos_pago_qr").fetchone()
    if (row_qr["c"] if isinstance(row_qr, dict) else row_qr[0]) == 0:
        db.execute("INSERT INTO metodos_pago_qr (sede_id, titulo, banco, telefono, rif_cedula, qr_base64, activo) VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (None, 'Pago Móvil Principal', 'Banesco (0134)', '0424-5371079', 'V-19000000', '', True if USE_POSTGRES else 1))

    db.commit()
    db.close()

try:
    init_db()
except Exception as e:
    print(f">> [WARN] Error inicializando BD: {e}")


ULTIMA_CONSULTA_BCV = 0
CACHE_BCV = None

def obtener_tasa_bcv_en_vivo():
    global ULTIMA_CONSULTA_BCV, CACHE_BCV
    ahora = time.time()
    if CACHE_BCV and (ahora - ULTIMA_CONSULTA_BCV < 3600):
        return CACHE_BCV
    endpoints = [
        "https://ve.dolarapi.com/v1/dolares/oficial",
        "https://pydolarvenezuela-api.vercel.app/api/v1/dollar/page?page=bcv"
    ]
    for url in endpoints:
        try:
            res = requests.get(url, timeout=3)
            if res.status_code == 200:
                data = res.json()
                tasa = None
                if "promedio" in data:
                    tasa = float(data["promedio"])
                elif "price" in data:
                    tasa = float(data["price"])
                elif isinstance(data, dict) and "monitors" in data and "bcv" in data["monitors"]:
                    tasa = float(data["monitors"]["bcv"]["price"])
                if tasa and tasa > 0:
                    CACHE_BCV = tasa
                    ULTIMA_CONSULTA_BCV = ahora
                    db = get_db()
                    db.execute("UPDATE configuracion SET tasa_bcv = ? WHERE id = 1", (tasa,))
                    db.commit()
                    db.close()
                    return tasa
        except Exception:
            continue
    db = get_db()
    row = db.execute("SELECT tasa_bcv FROM configuracion WHERE id = 1").fetchone()
    db.close()
    if row and row["tasa_bcv"]:
        return float(row["tasa_bcv"])
    return 45.50

# ----------------------------------------------------------------------
# AUTENTICACIÓN Y PERMISOS
# ----------------------------------------------------------------------
INTENTOS_LOGIN = {}
MAX_INTENTOS = 5
BLOQUEO_SEG = 300
_LOCK_INTENTOS = threading.Lock()
_HASH_FALSO = generate_password_hash("sin-usuario")


def _clave_intento(username):
    ip = (request.headers.get("X-Forwarded-For", request.remote_addr or "") or "").split(",")[0].strip()
    return f"{ip}|{username.lower()}"


def _segundos_bloqueo(clave):
    with _LOCK_INTENTOS:
        _, hasta = INTENTOS_LOGIN.get(clave, (0, 0))
        if hasta and time.time() < hasta:
            return int(hasta - time.time()) + 1
        if hasta:
            INTENTOS_LOGIN.pop(clave, None)
    return 0


def _registrar_fallo(clave):
    with _LOCK_INTENTOS:
        n, _ = INTENTOS_LOGIN.get(clave, (0, 0))
        n += 1
        INTENTOS_LOGIN[clave] = (n, time.time() + BLOQUEO_SEG if n >= MAX_INTENTOS else 0)


def usuario_actual():
    uid = session.get("user_id")
    if not uid:
        return None
    db = get_db()
    row = db.execute("SELECT id, nombre, username, rol, sede_id, activo FROM usuarios WHERE id = ?", (uid,)).fetchone()
    db.close()
    if not row or not row["activo"]:
        session.clear()
        return None
    return dict(row)


def requiere_login(roles=None):
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            u = usuario_actual()
            if not u:
                return jsonify({"success": False, "error": "Inicia sesión para continuar."}), 401
            if roles and u["rol"] not in roles:
                return jsonify({"success": False, "error": "No tienes permiso para esta acción."}), 403
            g.user = u
            return fn(*args, **kwargs)
        return wrapper
    return deco


def _num(v, nombre, minimo=None):
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{nombre} no es un número válido.")
    if x != x or x in (float("inf"), float("-inf")):
        raise ValueError(f"{nombre} no es un número válido.")
    if minimo is not None and x < minimo:
        raise ValueError(f"{nombre} debe ser mayor o igual a {minimo:g}.")
    return x


def limpiar_mods(mods):
    if not isinstance(mods, list):
        return []
    return [str(m).strip()[:60] for m in mods if str(m).strip()][:12]


def ajustar_stock(db, sede_id, insumo_id, delta, permitir_negativo=False):
    row = db.execute("SELECT stock_actual FROM inventario_por_sede WHERE sede_id = ? AND insumo_id = ?", (sede_id, insumo_id)).fetchone()
    if row is None:
        nuevo = delta if permitir_negativo else max(0.0, delta)
        db.execute("INSERT INTO inventario_por_sede (sede_id, insumo_id, stock_actual) VALUES (?, ?, ?)", (sede_id, insumo_id, nuevo))
        return nuevo
    nuevo = float(row["stock_actual"]) + delta
    if not permitir_negativo:
        nuevo = max(0.0, nuevo)
    db.execute("UPDATE inventario_por_sede SET stock_actual = ? WHERE sede_id = ? AND insumo_id = ?", (nuevo, sede_id, insumo_id))
    return nuevo


def _datos_usuario(u):
    return {"id": u["id"], "nombre": u["nombre"], "username": u["username"], "rol": u["rol"], "sede_id": u["sede_id"]}


@app.route("/api/auth/login", methods=["POST"])
def api_login():
    data = request.get_json(silent=True) or {}
    username = str(data.get("username", "")).strip()
    pin = str(data.get("pin", "")).strip()
    if not username or not pin:
        return jsonify({"success": False, "error": "Escribe tu usuario y tu PIN."}), 400

    clave = _clave_intento(username)
    espera = _segundos_bloqueo(clave)
    if espera:
        minutos = (espera + 59) // 60
        return jsonify({"success": False, "error": f"Demasiados intentos. Prueba de nuevo en {minutos} min."}), 429

    db = get_db()
    row = db.execute("SELECT * FROM usuarios WHERE LOWER(username) = LOWER(?) AND activo = 1", (username,)).fetchone()
    db.close()

    if row:
        ok = check_password_hash(row["pin_hash"], pin)
    else:
        check_password_hash(_HASH_FALSO, pin)  # mismo tiempo de respuesta exista o no el usuario
        ok = False

    if not ok:
        _registrar_fallo(clave)
        return jsonify({"success": False, "error": "Usuario o PIN incorrecto."}), 401

    with _LOCK_INTENTOS:
        INTENTOS_LOGIN.pop(clave, None)
    session.clear()
    session.permanent = True
    session["user_id"] = row["id"]
    return jsonify({"success": True, "user": _datos_usuario(dict(row))})


@app.route("/api/auth/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"success": True})


@app.route("/api/auth/me", methods=["GET"])
@requiere_login()
def api_me():
    return jsonify({"success": True, "user": _datos_usuario(g.user)})


@app.route("/api/auth/cambiar-pin", methods=["POST"])
@requiere_login()
def api_cambiar_pin():
    data = request.get_json(silent=True) or {}
    nuevo = str(data.get("pin_nuevo", "")).strip()
    if not pin_valido(nuevo):
        return jsonify({"success": False, "error": MSG_PIN}), 400
    objetivo = data.get("user_id")
    db = get_db()
    if objetivo and int(objetivo) != g.user["id"]:
        if g.user["rol"] != "superusuario":
            db.close()
            return jsonify({"success": False, "error": "No tienes permiso para esta acción."}), 403
        target_id = int(objetivo)
        existe = db.execute("SELECT id FROM usuarios WHERE id = ?", (target_id,)).fetchone()
        if not existe:
            db.close()
            return jsonify({"success": False, "error": "Usuario no encontrado."}), 404
    else:
        target_id = g.user["id"]
        row = db.execute("SELECT pin_hash FROM usuarios WHERE id = ?", (target_id,)).fetchone()
        if not row or not check_password_hash(row["pin_hash"], str(data.get("pin_actual", "")).strip()):
            db.close()
            return jsonify({"success": False, "error": "El PIN actual no es correcto."}), 400
    db.execute("UPDATE usuarios SET pin_hash = ?, pin_plain = '' WHERE id = ?", (generate_password_hash(nuevo), target_id))
    db.commit()
    db.close()
    return jsonify({"success": True})


@app.route("/api/init", methods=["GET"])
@requiere_login()
def api_init():
    es_super = g.user["rol"] == "superusuario"
    db = get_db()
    cfg = dict(db.execute("SELECT * FROM configuracion WHERE id = 1").fetchone())
    sedes = [dict(s) for s in db.execute("SELECT * FROM sedes WHERE activa = 1 ORDER BY id ASC").fetchall()]
    usuarios_raw = [dict(u) for u in db.execute("SELECT * FROM usuarios WHERE activo = 1 ORDER BY id ASC").fetchall()]
    usuarios = [{
        "id": u["id"],
        "nombre": u["nombre"],
        "username": u["username"],
        "rol": u["rol"],
        "sede_id": u["sede_id"],
        "activo": bool(u["activo"])
    } for u in usuarios_raw if es_super or u["id"] == g.user["id"]]
    insumos = [dict(i) for i in db.execute("SELECT * FROM insumos_maestro ORDER BY id ASC").fetchall()]
    productos = [dict(p) for p in db.execute("SELECT * FROM catalogo_productos WHERE activo = 1 ORDER BY id ASC").fetchall()]
    recetas = [dict(r) for r in db.execute("SELECT * FROM recetas_bom").fetchall()]
    inv_rows = [dict(r) for r in db.execute("SELECT sede_id, insumo_id, stock_actual FROM inventario_por_sede").fetchall()]
    if not es_super:
        inv_rows = [r for r in inv_rows if r["sede_id"] == g.user["sede_id"]]
    qrs = [dict(q) for q in db.execute("SELECT * FROM metodos_pago_qr WHERE activo = 1 ORDER BY id ASC").fetchall()]
    if es_super:
        ordenes_rows = [dict(o) for o in db.execute("SELECT * FROM ordenes ORDER BY id DESC LIMIT 200").fetchall()]
    else:
        ordenes_rows = [dict(o) for o in db.execute("SELECT * FROM ordenes WHERE sede_id = ? ORDER BY id DESC LIMIT 200", (g.user["sede_id"],)).fetchall()]
    _ids_ordenes = {o["id"] for o in ordenes_rows}
    items_rows = [dict(it) for it in db.execute("SELECT * FROM orden_items").fetchall()]
    items_rows = [it for it in items_rows if it["orden_id"] in _ids_ordenes]
    logs_rows = [dict(l) for l in db.execute("SELECT * FROM auditoria_logs ORDER BY id DESC LIMIT 50").fetchall()] if es_super else []
    db.close()

    # Armar mapa de recetas por producto
    rec_map = {}
    for r in recetas:
        pid = r["producto_id"]
        if pid not in rec_map:
            rec_map[pid] = []
        rec_map[pid].append({"insumo_id": r["insumo_id"], "cantidad": float(r["cantidad_consumida"])})

    for p in productos:
        p["precio_venta_usd"] = float(p["precio_venta_usd"])
        p["activo"] = bool(p["activo"])
        p["receta"] = rec_map.get(p["id"], [])

    for i in insumos:
        i["costo_paquete_usd"] = float(i["costo_paquete_usd"])
        i["rendimiento_paquete"] = float(i["rendimiento_paquete"])
        i["costo_unitario_usd"] = float(i["costo_unitario_usd"])
        i["alerta_stock_minimo"] = float(i["alerta_stock_minimo"])

    inv_map = {}
    for row in inv_rows:
        inv_map[f"{row['sede_id']}_{row['insumo_id']}"] = float(row["stock_actual"])

    user_map = {u["id"]: u["nombre"] for u in usuarios_raw}
    prod_map = {p["id"]: p["nombre"] for p in productos}

    items_by_order = {}
    for it in items_rows:
        oid = it["orden_id"]
        if oid not in items_by_order:
            items_by_order[oid] = []
        try:
            mods = json.loads(it["modificadores_json"]) if it.get("modificadores_json") else []
        except Exception:
            mods = []
        items_by_order[oid].append({
            "producto_id": it["producto_id"],
            "nombre": prod_map.get(it["producto_id"], f"Prod #{it['producto_id']}"),
            "cantidad": int(it["cantidad"]),
            "precio_unitario_usd": float(it["precio_unitario_usd"]),
            "costo_unitario_usd": float(it["costo_unitario_usd"]),
            "modificadores": mods
        })

    ordenes_list = []
    for o in ordenes_rows:
        raw_dt = str(o.get("created_at") or datetime.datetime.now().isoformat())
        fecha_corta = raw_dt[:10]
        hora_corta = raw_dt[11:16] if len(raw_dt) >= 16 else raw_dt
        ordenes_list.append({
            "id": o["id"],
            "sede_id": o["sede_id"],
            "cajero_id": o["cajero_id"],
            "cajero_nombre": user_map.get(o["cajero_id"], "Cajero"),
            "cliente_nombre": o["cliente_nombre"] or "Consumidor",
            "cliente_telefono": o["cliente_telefono"] or "",
            "total_venta_usd": float(o["total_venta_usd"]),
            "costo_insumos_usd": float(o["costo_insumos_usd"]),
            "tasa_aplicada": float(o["tasa_aplicada"]),
            "tasa_reposicion_momento": float(o["tasa_reposicion_momento"]),
            "recibido_usd": float(o["recibido_usd"]),
            "recibido_bs": float(o["recibido_bs"]),
            "vuelto_usd": float(o["vuelto_usd"]),
            "vuelto_bs": float(o["vuelto_bs"]),
            "estado_pago": o["estado_pago"],
            "estado_cocina": o["estado_cocina"],
            "notas_cocina": o["notas_cocina"] or "",
            "items": items_by_order.get(o["id"], []),
            "created_at": hora_corta,
            "fecha_corta": fecha_corta
        })

    return jsonify({
        "success": True,
        "motor_bd": "PostgreSQL Cloud (Supabase)" if USE_POSTGRES else "SQLite Local (Configura DATABASE_URL para persistencia Cloud)",
        "config": {
            "tasa_bcv": float(cfg["tasa_bcv"]),
            "tasa_manual": float(cfg["tasa_manual"]),
            "tasa_reposicion": float(cfg["tasa_reposicion"]),
            "usar_tasa_manual": bool(cfg["usar_tasa_manual"]),
            "pct_reserva_operativa": float(cfg["pct_reserva_operativa"]),
            "gastos_operativos_fijos": float(cfg.get("gastos_operativos_fijos", 0.0))
        },
        "sedes": sedes,
        "usuarios": usuarios,
        "insumos": insumos,
        "productos": productos,
        "inventario_sedes": inv_map,
        "metodos_qr": qrs,
        "ordenes": ordenes_list,
        "auditoria_logs": logs_rows
    })

@app.route("/api/tasa/sync", methods=["POST"])
@requiere_login()
def api_tasa_sync():
    data = request.get_json(silent=True) or {}
    db = get_db()
    if any(k in data for k in ["tasa_manual", "usar_tasa_manual", "tasa_reposicion", "pct_reserva_operativa", "gastos_operativos_fijos"]):
        if g.user["rol"] != "superusuario":
            db.close()
            return jsonify({"success": False, "error": "Solo el superusuario puede cambiar las tasas."}), 403
        actual = dict(db.execute("SELECT * FROM configuracion WHERE id = 1").fetchone())
        try:
            t_man = _num(data.get("tasa_manual", actual["tasa_manual"]), "La tasa manual", 0.0001)
            t_rep = _num(data.get("tasa_reposicion", actual["tasa_reposicion"]), "La tasa de reposición", 0.0001)
            pct_r = _num(data.get("pct_reserva_operativa", actual["pct_reserva_operativa"]), "El % de reserva", 0)
            g_fij = _num(data.get("gastos_operativos_fijos", actual.get("gastos_operativos_fijos", 0.0)), "Los gastos fijos", 0)
        except ValueError as e:
            db.close()
            return jsonify({"success": False, "error": str(e)}), 400
        usar = bool(data.get("usar_tasa_manual", actual["usar_tasa_manual"]))
        u_man = usar if USE_POSTGRES else (1 if usar else 0)
        db.execute("""
            UPDATE configuracion
            SET tasa_manual = ?, usar_tasa_manual = ?, tasa_reposicion = ?, pct_reserva_operativa = ?, gastos_operativos_fijos = ?
            WHERE id = 1
        """, (t_man, u_man, t_rep, pct_r, g_fij))
        db.commit()
    tasa_bcv = obtener_tasa_bcv_en_vivo()
    cfg = dict(db.execute("SELECT * FROM configuracion WHERE id = 1").fetchone())
    db.close()
    return jsonify({
        "success": True,
        "tasa_bcv": float(cfg["tasa_bcv"]),
        "tasa_manual": float(cfg["tasa_manual"]),
        "tasa_reposicion": float(cfg["tasa_reposicion"]),
        "usar_tasa_manual": bool(cfg["usar_tasa_manual"]),
        "pct_reserva_operativa": float(cfg["pct_reserva_operativa"])
    })

@app.route("/api/orden/crear", methods=["POST"])
@requiere_login()
def api_orden_crear():
    data = request.get_json(silent=True) or {}
    if g.user["rol"] == "cajero":
        sede_id = int(g.user["sede_id"] or 0)  # un cajero solo vende en su sede
    else:
        sede_id = int(data.get("sede_id") or 0)
    cajero_id = g.user["id"]  # siempre el usuario de la sesión, nunca el que diga el navegador
    if not sede_id:
        return jsonify({"success": False, "error": "Sede no válida"}), 400
    items = data.get("items", [])
    recibido_usd = float(data.get("recibido_usd", 0))
    recibido_bs = float(data.get("recibido_bs", 0))
    vuelto_usd = float(data.get("vuelto_usd", 0))
    vuelto_bs = float(data.get("vuelto_bs", 0))
    estado_pago = data.get("estado_pago", "PAGADA")
    if estado_pago not in ("PAGADA", "FIADO"):
        estado_pago = "PAGADA"
    cliente_nombre = data.get("cliente_nombre", "Consumidor")
    cliente_telefono = data.get("cliente_telefono", "")
    notas_cocina = data.get("notas_cocina", "")

    if not items:
        return jsonify({"success": False, "error": "Orden vacía"}), 400

    db = get_db()
    cfg = dict(db.execute("SELECT * FROM configuracion WHERE id = 1").fetchone())
    tasa_aplicada = float(cfg["tasa_manual"] if cfg["usar_tasa_manual"] else cfg["tasa_bcv"])
    tasa_reposicion = float(cfg["tasa_reposicion"])

    insumos_map = {r["id"]: float(r["costo_unitario_usd"]) for r in db.execute("SELECT id, costo_unitario_usd FROM insumos_maestro").fetchall()}

    total_venta_usd = 0.0
    costo_total_insumos_usd = 0.0
    desglose_insumos = {}
    items_para_insertar = []

    for it in items:
        p_id = int(it["producto_id"])
        cant = int(it.get("cantidad", 1))
        if cant < 1:
            continue
        prod = db.execute("SELECT * FROM catalogo_productos WHERE id = ?", (p_id,)).fetchone()
        if not prod:
            continue
        p_precio = float(prod["precio_venta_usd"])
        total_venta_usd += p_precio * cant

        boms = db.execute("SELECT insumo_id, cantidad_consumida FROM recetas_bom WHERE producto_id = ?", (p_id,)).fetchall()
        costo_unit_prod = 0.0
        for b in boms:
            iid = b["insumo_id"]
            c_cons = float(b["cantidad_consumida"])
            costo_unit_prod += insumos_map.get(iid, 0.0) * c_cons
            desglose_insumos[iid] = desglose_insumos.get(iid, 0.0) + (c_cons * cant)

        costo_total_insumos_usd += (costo_unit_prod * cant)
        items_para_insertar.append((p_id, cant, p_precio, costo_unit_prod, json.dumps(limpiar_mods(it.get("modificadores", [])))))

    if not items_para_insertar:
        db.close()
        return jsonify({"success": False, "error": "Orden vacía"}), 400

    ahora_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    orden_id = db.insert_returning_id("""
        INSERT INTO ordenes (
            sede_id, cajero_id, cliente_nombre, cliente_telefono,
            total_venta_usd, costo_insumos_usd, tasa_aplicada, tasa_reposicion_momento,
            recibido_usd, recibido_bs, vuelto_usd, vuelto_bs,
            estado_pago, estado_cocina, notas_cocina, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PREPARANDO', ?, ?)
    """, (
        sede_id, cajero_id, cliente_nombre, cliente_telefono,
        total_venta_usd, costo_total_insumos_usd, tasa_aplicada, tasa_reposicion,
        recibido_usd, recibido_bs, vuelto_usd, vuelto_bs,
        estado_pago, notas_cocina, ahora_str
    ))

    for p_id, cant, p_precio, c_unit, mods_json in items_para_insertar:
        db.execute("""
            INSERT INTO orden_items (orden_id, producto_id, cantidad, precio_unitario_usd, costo_unitario_usd, modificadores_json)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (orden_id, p_id, cant, p_precio, c_unit, mods_json))

    for iid, cant_desc in desglose_insumos.items():
        ajustar_stock(db, sede_id, iid, -cant_desc)
        db.execute("""
            INSERT INTO movimientos_kardex (sede_id, insumo_id, usuario_id, tipo, cantidad, costo_unitario_momento, motivo, created_at)
            VALUES (?, ?, ?, 'VENTA', ?, ?, ?, ?)
        """, (sede_id, iid, cajero_id, -cant_desc, insumos_map.get(iid, 0.0), f"Venta Orden #{orden_id}", ahora_str))

    db.commit()
    db.close()
    return jsonify({"success": True, "orden_id": orden_id})

@app.route("/api/orden/estado", methods=["PATCH"])
@requiere_login()
def api_orden_estado():
    data = request.get_json(silent=True) or {}
    orden_id = data.get("orden_id")
    estado_cocina = data.get("estado_cocina")
    estado_pago = data.get("estado_pago")
    db = get_db()
    orden = db.execute("SELECT sede_id, estado_pago FROM ordenes WHERE id = ?", (orden_id,)).fetchone()
    if not orden:
        db.close()
        return jsonify({"success": False, "error": "Orden no encontrada"}), 404
    if g.user["rol"] == "cajero" and orden["sede_id"] != g.user["sede_id"]:
        db.close()
        return jsonify({"success": False, "error": "Esa orden es de otra sede."}), 403
    if estado_cocina:
        if estado_cocina not in ("PREPARANDO", "ENTREGADO"):
            db.close()
            return jsonify({"success": False, "error": "Estado de cocina inválido"}), 400
        db.execute("UPDATE ordenes SET estado_cocina = ? WHERE id = ?", (estado_cocina, orden_id))
    if estado_pago:
        # Solo se permite cobrar un fiado. Anular se hace por Auditoría (con justificación).
        if estado_pago != "PAGADA" or orden["estado_pago"] != "FIADO":
            db.close()
            return jsonify({"success": False, "error": "Cambio de estado de pago no permitido"}), 400
        db.execute("UPDATE ordenes SET estado_pago = ? WHERE id = ?", (estado_pago, orden_id))
    db.commit()
    db.close()
    return jsonify({"success": True})

@app.route("/api/auditoria/corregir", methods=["POST"])
@requiere_login(roles=("superusuario",))
def api_auditoria_corregir():
    data = request.json or {}
    orden_id = int(data.get("orden_id"))
    justificacion = str(data.get("justificacion", "")).strip()
    superusuario_id = g.user["id"]

    if not justificacion:
        return jsonify({"success": False, "error": "Justificación obligatoria"}), 400

    db = get_db()
    orden = db.execute("SELECT * FROM ordenes WHERE id = ?", (orden_id,)).fetchone()
    if not orden:
        db.close()
        return jsonify({"success": False, "error": "Orden no encontrada"}), 404

    orden_dict = dict(orden)
    if orden_dict["estado_pago"] == "ANULADA":
        db.close()
        return jsonify({"success": False, "error": "Ya está anulada"}), 400

    sede_id = orden_dict["sede_id"]
    items = db.execute("SELECT * FROM orden_items WHERE orden_id = ?", (orden_id,)).fetchall()
    insumos_rev = {}
    for it in items:
        boms = db.execute("SELECT insumo_id, cantidad_consumida FROM recetas_bom WHERE producto_id = ?", (it["producto_id"],)).fetchall()
        for b in boms:
            iid = b["insumo_id"]
            insumos_rev[iid] = insumos_rev.get(iid, 0.0) + (float(b["cantidad_consumida"]) * int(it["cantidad"]))

    ahora_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for iid, cant_ret in insumos_rev.items():
        db.execute("UPDATE inventario_por_sede SET stock_actual = stock_actual + ? WHERE sede_id = ? AND insumo_id = ?", (cant_ret, sede_id, iid))
        db.execute("""
            INSERT INTO movimientos_kardex (sede_id, insumo_id, usuario_id, tipo, cantidad, costo_unitario_momento, motivo, created_at)
            VALUES (?, ?, ?, 'REVERSION_AUDITORIA', ?, 0, ?, ?)
        """, (sede_id, iid, superusuario_id, cant_ret, f"Reversión Orden #{orden_id}: {justificacion}", ahora_str))

    db.execute("UPDATE ordenes SET estado_pago = 'ANULADA' WHERE id = ?", (orden_id,))
    db.execute("""
        INSERT INTO auditoria_logs (superusuario_id, sede_id, accion, entidad_afectada, registro_id, snapshot_anterior_json, snapshot_nuevo_json, justificacion, created_at)
        VALUES (?, ?, 'ANULAR_ORDEN', 'ordenes', ?, ?, ?, ?, ?)
    """, (superusuario_id, sede_id, orden_id, json.dumps(orden_dict, default=str), json.dumps({"estado_pago": "ANULADA"}), justificacion, ahora_str))

    db.commit()
    db.close()
    return jsonify({"success": True})

@app.route("/api/inventario/movimiento", methods=["POST"])
@requiere_login()
def api_inventario_movimiento():
    data = request.get_json(silent=True) or {}
    es_super = g.user["rol"] == "superusuario"
    tipo = str(data.get("tipo", "")).upper()
    if tipo not in ("MERMA", "CONSUMO", "COMPRA", "TRANSFERENCIA", "AJUSTE"):
        return jsonify({"success": False, "error": "Tipo de movimiento inválido."}), 400
    if not es_super and tipo not in ("MERMA", "CONSUMO"):
        return jsonify({"success": False, "error": "Solo el superusuario puede registrar compras, ajustes o transferencias."}), 403

    db = get_db()
    try:
        insumo_id = int(data.get("insumo_id") or 0)
        sede_id = int(data.get("sede_id") or 0) if es_super else int(g.user["sede_id"] or 0)
        ins = db.execute("SELECT * FROM insumos_maestro WHERE id = ?", (insumo_id,)).fetchone()
        sede = db.execute("SELECT id FROM sedes WHERE id = ?", (sede_id,)).fetchone()
        if not ins or not sede:
            raise ValueError("Insumo o sede no encontrados.")
        costo = float(ins["costo_unitario_usd"])
        motivo = str(data.get("motivo", "")).strip()[:200]
        ahora = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        fila = db.execute("SELECT stock_actual FROM inventario_por_sede WHERE sede_id = ? AND insumo_id = ?", (sede_id, insumo_id)).fetchone()
        stock_antes = float(fila["stock_actual"]) if fila else 0.0

        def kardex(sede_k, tipo_k, cant, mot):
            db.execute(
                "INSERT INTO movimientos_kardex (sede_id, insumo_id, usuario_id, tipo, cantidad, costo_unitario_momento, motivo, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (sede_k, insumo_id, g.user["id"], tipo_k, cant, costo, mot, ahora))

        if tipo in ("MERMA", "CONSUMO"):
            cant = _num(data.get("cantidad"), "La cantidad", 0.0001)
            if cant > stock_antes + 1e-9:
                raise ValueError(f"Stock insuficiente: hay {stock_antes:g} y quieres descontar {cant:g}.")
            ajustar_stock(db, sede_id, insumo_id, -cant)
            kardex(sede_id, tipo, -cant, motivo or "Merma reportada")

        elif tipo == "COMPRA":
            cant = _num(data.get("cantidad"), "La cantidad", 0.0001)
            cp = data.get("costo_paquete_usd")
            if cp not in (None, ""):
                cp = _num(cp, "El costo del paquete", 0.0001)
                costo = cp / float(ins["rendimiento_paquete"])
                db.execute("UPDATE insumos_maestro SET costo_paquete_usd = ?, costo_unitario_usd = ? WHERE id = ?", (cp, costo, insumo_id))
            ajustar_stock(db, sede_id, insumo_id, cant)
            kardex(sede_id, "COMPRA", cant, motivo or "Compra / entrada de mercancía")

        elif tipo == "TRANSFERENCIA":
            cant = _num(data.get("cantidad"), "La cantidad", 0.0001)
            destino = int(data.get("sede_destino_id") or 0)
            if destino == sede_id:
                raise ValueError("El destino debe ser distinto al origen.")
            if not db.execute("SELECT id FROM sedes WHERE id = ?", (destino,)).fetchone():
                raise ValueError("Sede destino no encontrada.")
            if cant > stock_antes + 1e-9:
                raise ValueError(f"Stock insuficiente en el origen: hay {stock_antes:g}.")
            ajustar_stock(db, sede_id, insumo_id, -cant)
            ajustar_stock(db, destino, insumo_id, cant)
            kardex(sede_id, "TRANSFERENCIA", -cant, f"Envío a sede {destino}: {motivo}".rstrip(": "))
            kardex(destino, "TRANSFERENCIA", cant, f"Recibido de sede {sede_id}: {motivo}".rstrip(": "))

        else:  # AJUSTE por conteo físico
            objetivo = _num(data.get("nuevo_stock"), "El conteo real", 0)
            if not motivo:
                raise ValueError("El motivo del ajuste es obligatorio.")
            delta = objetivo - stock_antes
            if abs(delta) > 1e-9:
                ajustar_stock(db, sede_id, insumo_id, delta)
                tipo_k = "AJUSTE" if KARDEX_ACEPTA_AJUSTE else ("COMPRA" if delta > 0 else "MERMA")
                kardex(sede_id, tipo_k, delta, f"Ajuste de conteo físico: {motivo}")
                db.execute(
                    "INSERT INTO auditoria_logs (superusuario_id, sede_id, accion, entidad_afectada, registro_id, snapshot_anterior_json, snapshot_nuevo_json, justificacion, created_at) VALUES (?, ?, 'AJUSTE_STOCK', 'inventario_por_sede', ?, ?, ?, ?, ?)",
                    (g.user["id"], sede_id, insumo_id, json.dumps({"stock_actual": stock_antes}), json.dumps({"stock_actual": objetivo}), motivo, ahora))

        db.commit()
        fila = db.execute("SELECT stock_actual FROM inventario_por_sede WHERE sede_id = ? AND insumo_id = ?", (sede_id, insumo_id)).fetchone()
        return jsonify({"success": True, "stock_actual": float(fila["stock_actual"]) if fila else 0.0})
    except ValueError as e:
        db.conn.rollback()
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        db.conn.rollback()
        print(f">> [ERROR] inventario/movimiento: {e}")
        return jsonify({"success": False, "error": "Error interno al registrar el movimiento."}), 500
    finally:
        db.close()


@app.route("/api/admin/crud", methods=["POST"])
@requiere_login(roles=("superusuario",))
def api_admin_crud():
    data = request.get_json(silent=True) or {}
    entidad = data.get("entidad")
    accion = data.get("accion")
    reg = data.get("registro") or {}
    verdadero = True if USE_POSTGRES else 1
    falso = False if USE_POSTGRES else 0
    db = get_db()
    resultado = {}
    try:
        if entidad == "usuarios" and accion == "CREATE":
            nombre = str(reg.get("nombre", "")).strip()
            username = str(reg.get("username", "")).strip().lower()
            pin = str(reg.get("pin", "")).strip()
            rol = reg.get("rol", "cajero")
            sede = reg.get("sede_id") or None
            if rol not in ("cajero", "superusuario"):
                raise ValueError("Rol inválido.")
            if not nombre or not username:
                raise ValueError("Nombre y usuario son obligatorios.")
            if not pin_valido(pin):
                raise ValueError(MSG_PIN)
            if rol == "cajero" and not sede:
                raise ValueError("Un cajero necesita una sede asignada.")
            if db.execute("SELECT id FROM usuarios WHERE LOWER(username) = ?", (username,)).fetchone():
                raise ValueError("Ese nombre de usuario ya existe.")
            db.execute("INSERT INTO usuarios (nombre, username, pin_plain, pin_hash, rol, sede_id, activo) VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (nombre, username, '', generate_password_hash(pin), rol, int(sede) if sede else None, verdadero))

        elif entidad == "sedes" and accion == "CREATE":
            nombre = str(reg.get("nombre", "")).strip()
            if not nombre:
                raise ValueError("El nombre de la sede es obligatorio.")
            new_id = db.insert_returning_id("INSERT INTO sedes (nombre, es_almacen, activa) VALUES (?, ?, ?)", (nombre, falso, verdadero))
            for i in db.execute("SELECT id FROM insumos_maestro").fetchall():
                db.execute("INSERT INTO inventario_por_sede (sede_id, insumo_id, stock_actual) VALUES (?, ?, 0)", (new_id, i["id"]))

        elif entidad == "metodos_qr" and accion == "UPDATE":
            qr = str(reg.get("qr_base64", "") or "")
            if len(qr) > 3_000_000:
                raise ValueError("La imagen del QR es demasiado grande (máx. ~2 MB).")
            db.execute("UPDATE metodos_pago_qr SET titulo = ?, banco = ?, telefono = ?, rif_cedula = ?, qr_base64 = ? WHERE id = ?",
                       (str(reg.get("titulo", "Pago Móvil"))[:120], str(reg.get("banco", ""))[:100], str(reg.get("telefono", ""))[:30],
                        str(reg.get("rif_cedula", ""))[:30], qr, int(reg.get("id") or 1)))

        elif entidad == "insumos" and accion in ("CREATE", "UPDATE"):
            nombre = str(reg.get("nombre", "")).strip()[:120]
            unidad = str(reg.get("unidad", "und")).strip()[:30] or "und"
            if not nombre:
                raise ValueError("El nombre del insumo es obligatorio.")
            cp = _num(reg.get("costo_paquete_usd"), "El costo del paquete", 0)
            rp = _num(reg.get("rendimiento_paquete"), "El rendimiento por paquete", 0.0001)
            alerta = _num(reg.get("alerta_stock_minimo", 10), "La alerta de stock mínimo", 0)
            cu = cp / rp
            if accion == "UPDATE":
                iid = int(reg.get("id") or 0)
                if not db.execute("SELECT id FROM insumos_maestro WHERE id = ?", (iid,)).fetchone():
                    raise ValueError("Insumo no encontrado.")
                db.execute("UPDATE insumos_maestro SET nombre = ?, unidad = ?, costo_paquete_usd = ?, rendimiento_paquete = ?, costo_unitario_usd = ?, alerta_stock_minimo = ? WHERE id = ?",
                           (nombre, unidad, cp, rp, cu, alerta, iid))
            else:
                iid = db.insert_returning_id(
                    "INSERT INTO insumos_maestro (nombre, unidad, costo_paquete_usd, rendimiento_paquete, costo_unitario_usd, alerta_stock_minimo) VALUES (?, ?, ?, ?, ?, ?)",
                    (nombre, unidad, cp, rp, cu, alerta))
                stock_ini = _num(reg.get("stock_inicial") or 0, "El stock inicial", 0)
                sede_ini = int(reg.get("sede_id") or 0)
                for sd in db.execute("SELECT id FROM sedes").fetchall():
                    cant = stock_ini if sd["id"] == sede_ini else 0
                    db.execute("INSERT INTO inventario_por_sede (sede_id, insumo_id, stock_actual) VALUES (?, ?, ?)", (sd["id"], iid, cant))
                if stock_ini > 0 and sede_ini:
                    db.execute(
                        "INSERT INTO movimientos_kardex (sede_id, insumo_id, usuario_id, tipo, cantidad, costo_unitario_momento, motivo, created_at) VALUES (?, ?, ?, 'COMPRA', ?, ?, ?, ?)",
                        (sede_ini, iid, g.user["id"], stock_ini, cu, "Stock inicial al crear el insumo", datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            resultado["id"] = iid

        elif entidad == "productos" and accion == "UPDATE":
            precio = _num(reg.get("precio_venta_usd"), "El precio", 0)
            db.execute("UPDATE catalogo_productos SET precio_venta_usd = ? WHERE id = ?", (precio, int(reg.get("id"))))

        else:
            raise ValueError("Operación no soportada.")

        db.commit()
        return jsonify({"success": True, **resultado})
    except ValueError as e:
        db.conn.rollback()
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        db.conn.rollback()
        print(f">> [ERROR] admin/crud: {e}")
        return jsonify({"success": False, "error": "Error interno al guardar."}), 500
    finally:
        db.close()


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="es" class="dark">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no" />
  <title>Burger Admin • Multisede & Auditoría</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script crossorigin src="https://unpkg.com/react@18/umd/react.production.min.js"></script>
  <script crossorigin src="https://unpkg.com/react-dom@18/umd/react-dom.production.min.js"></script>
  <script src="https://unpkg.com/@babel/standalone/babel.min.js"></script>
  <style>
    body { background-color: #09090b; color: #ffffff; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; -webkit-tap-highlight-color: transparent; }
    .no-scrollbar::-webkit-scrollbar { display: none; }
    .no-scrollbar { -ms-overflow-style: none; scrollbar-width: none; }
  </style>
</head>
<body class="bg-[#09090b] text-white">
  <div id="root"></div>
  <script type="text/babel">
    const { useState, useEffect, useMemo, useRef } = React;

    // Generador ligero de iconos SVG vectoriales de alto contraste
    // Si el servidor responde 401 (sesión vencida), volvemos a la pantalla de login
    const _fetchOriginal = window.fetch.bind(window);
    window.fetch = async (...args) => {
      const res = await _fetchOriginal(...args);
      const url = String(args[0] && args[0].url ? args[0].url : args[0]);
      if (res.status === 401 && !url.includes('/api/auth/')) {
        window.dispatchEvent(new Event('sesion-expirada'));
      }
      return res;
    };

    const createIcon = (pathD) => ({ className = "w-4 h-4" }) => (
      <svg className={className} fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" viewBox="0 0 24 24">
        <path d={pathD} />
      </svg>
    );

    const ShoppingBag = createIcon("M6 2L3 6v14a2 2 0 002 2h14a2 2 0 002-2V6l-3-4z M3 6h18 M16 10a4 4 0 01-8 0");
    const ChefHat = createIcon("M6 13.87A4 4 0 017.41 6a5.11 5.11 0 011.05-1.54 5 5 0 017.08 0A5.11 5.11 0 0116.59 6 4 4 0 0118 13.87V21H6Z M6 17h12");
    const Package = createIcon("M16.5 9.4l-9-5.19M21 16V8a2 2 0 00-1-1.73l-7-4a2 2 0 00-2 0l-7 4A2 2 0 003 8v8a2 2 0 001 1.73l7 4a2 2 0 002 0l7-4A2 2 0 0021 16z M3.27 6.96L12 12.01l8.73-5.05 M12 22.08V12");
    const ShieldCheck = createIcon("M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z M9 12l2 2 4-4");
    const Settings = createIcon("M12 15a3 3 0 100-6 3 3 0 000 6z M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 010 2.83 2 2 0 01-2.83 0l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-2 2 2 2 0 01-2-2v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 01-2.83 0 2 2 0 010-2.83l.06-.06a1.65 1.65 0 00.33-1.82 1.65 1.65 0 00-1.51-1H3a2 2 0 01-2-2 2 2 0 012-2h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 010-2.83 2 2 0 012.83 0l.06.06a1.65 1.65 0 001.82.33H9a1.65 1.65 0 001-1.51V3a2 2 0 012-2 2 2 0 012 2v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 012.83 0 2 2 0 010 2.83l-.06.06a1.65 1.65 0 00-.33 1.82V9a1.65 1.65 0 001.51 1H21a2 2 0 012 2 2 2 0 01-2 2h-.09a1.65 1.65 0 00-1.51 1z");
    const CreditCard = createIcon("M1 4h22v16H1z M1 10h22");
    const QrCode = createIcon("M3 3h6v6H3z M15 3h6v6h-6z M3 15h6v6H3z M15 15h2v2h-2z M19 15h2v2h-2z M15 19h2v2h-2z M19 19h2v2h-2z");
    const Share2 = createIcon("M18 8a3 3 0 100-6 3 3 0 000 6z M6 15a3 3 0 100-6 3 3 0 000 6z M18 22a3 3 0 100-6 3 3 0 000 6z M8.59 13.51l6.83 3.98 M15.41 6.51l-6.82 3.98");
    const Copy = createIcon("M9 9h13v13H9z M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1");
    const AlertTriangle = createIcon("M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z M12 9v4 M12 17h.01");
    const CheckCircle2 = createIcon("M12 22c5.523 0 10-4.477 10-10S17.523 2 12 2 2 6.477 2 12s4.477 10 10 10z M9 12l2 2 4-4");
    const RefreshCw = createIcon("M23 4v6h-6 M1 20v-6h6 M3.51 9a9 9 0 0114.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0020.49 15");
    const Plus = createIcon("M12 5v14 M5 12h14");
    const Minus = createIcon("M5 12h14");
    const Trash2 = createIcon("M3 6h18 M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2");
    const ArrowRight = createIcon("M5 12h14 M12 5l7 7-7 7");
    const TrendingUp = createIcon("M23 6l-9.5 9.5-5-5L1 18 M17 6h6v6");
    const DollarSign = createIcon("M12 1v22 M17 5H9.5a3.5 3.5 0 000 7h5a3.5 3.5 0 010 7H6");
    const Store = createIcon("M3 9l9-7 9 7v11a2 2 0 01-2 2H5a2 2 0 01-2-2z M9 22V12h6v10");
    const User = createIcon("M20 21v-2a4 4 0 00-4-4H8a4 4 0 00-4 4v2 M12 11a4 4 0 100-8 4 4 0 000 8z");
    const Users = createIcon("M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2 M9 11a4 4 0 100-8 4 4 0 000 8z M23 21v-2a4 4 0 00-3-3.87 M16 3.13a4 4 0 010 7.75");
    const Lock = createIcon("M5 11h14a2 2 0 012 2v7a2 2 0 01-2 2H5a2 2 0 01-2-2v-7a2 2 0 012-2z M7 11V7a5 5 0 0110 0v4");
    const Download = createIcon("M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4 M7 10l5 5 5-5 M12 15V3");
    const FileCode = createIcon("M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z M14 2v6h6 M10 13l-2 2 2 2 M14 17l2-2-2-2");
    const Check = createIcon("M20 6L9 17l-5-5");
    const Eye = createIcon("M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z M12 15a3 3 0 100-6 3 3 0 000 6z");
    const Sliders = createIcon("M4 21v-7 M4 10V3 M12 21v-9 M12 8V3 M20 21v-5 M20 12V3 M1 14h6 M9 8h6 M17 16h6");
    const RotateCcw = createIcon("M1 4v6h6 M3.51 15a9 9 0 102.13-9.36L1 10");
    const BarChart3 = createIcon("M18 20V10 M12 20V4 M6 20v-6");
    const Calendar = createIcon("M3 4h18v18H3z M16 2v4 M8 2v4 M3 10h18");
    const FileSpreadsheet = createIcon("M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z M14 2v6h6 M8 13h8 M8 17h8");
    const Printer = createIcon("M6 9V2h12v7 M6 18H4a2 2 0 01-2-2v-5a2 2 0 012-2h16a2 2 0 012 2v5a2 2 0 01-2 2h-2 M6 14h12v8H6z");
    const ChevronDown = createIcon("M6 9l6 6 6-6");
    const Layers = createIcon("M12 2L2 7l10 5 10-5-10-5z M2 17l10 5 10-5 M2 12l10 5 10-5");
    const Percent = createIcon("M19 5L5 19 M6.5 9a2.5 2.5 0 100-5 2.5 2.5 0 000 5z M17.5 20a2.5 2.5 0 100-5 2.5 2.5 0 000 5z");
    const Clock = createIcon("M12 22c5.523 0 10-4.477 10-10S17.523 2 12 2 2 6.477 2 12s4.477 10 10 10z M12 6v6l4 2");
    const ArrowUpRight = createIcon("M7 17L17 7 M7 7h10v10");
    const ArrowDownRight = createIcon("M7 7l10 10 M17 7v10H7");
    const Sparkles = createIcon("M12 3l1.9 5.8 5.8 1.9-5.8 1.9L12 18.4l-1.9-5.8-5.8-1.9 5.8-1.9z");
    const Search = createIcon("M11 19a8 8 0 100-16 8 8 0 000 16z M21 21l-4.35-4.35");
    const Filter = createIcon("M22 3H2l8 9.46V19l4 2v-8.54L22 3z");

// DATOS SEMILLA EXACTOS (SECCIÓN 4.3)
// ==========================================

const INSUMOS_SEMILLA = [
  { id: 1, nombre: 'Pan Pequeño', unidad: 'und', costo_paquete_usd: 1.98, rendimiento_paquete: 10, costo_unitario_usd: 0.198, alerta_stock_minimo: 20 },
  { id: 2, nombre: 'Pan Yumpi Grande', unidad: 'und', costo_paquete_usd: 2.30, rendimiento_paquete: 10, costo_unitario_usd: 0.23, alerta_stock_minimo: 15 },
  { id: 3, nombre: 'Salchicha', unidad: 'und', costo_paquete_usd: 3.00, rendimiento_paquete: 10, costo_unitario_usd: 0.30, alerta_stock_minimo: 20 },
  { id: 4, nombre: 'Salsa', unidad: 'porcion', costo_paquete_usd: 5.00, rendimiento_paquete: 250, costo_unitario_usd: 0.02, alerta_stock_minimo: 30 },
  { id: 5, nombre: 'Mayonesa', unidad: 'porcion', costo_paquete_usd: 5.00, rendimiento_paquete: 100, costo_unitario_usd: 0.05, alerta_stock_minimo: 25 },
  { id: 6, nombre: 'Mostaza', unidad: 'porcion', costo_paquete_usd: 3.00, rendimiento_paquete: 300, costo_unitario_usd: 0.01, alerta_stock_minimo: 30 },
  { id: 7, nombre: 'Base Salsa', unidad: 'porcion', costo_paquete_usd: 6.00, rendimiento_paquete: 150, costo_unitario_usd: 0.04, alerta_stock_minimo: 20 },
  { id: 8, nombre: 'Verduras', unidad: 'porcion', costo_paquete_usd: 4.00, rendimiento_paquete: 60, costo_unitario_usd: 0.0667, alerta_stock_minimo: 15 },
  { id: 9, nombre: 'Papitas', unidad: 'porcion', costo_paquete_usd: 3.76, rendimiento_paquete: 100, costo_unitario_usd: 0.0376, alerta_stock_minimo: 25 },
  { id: 10, nombre: 'Papel', unidad: 'und', costo_paquete_usd: 2.00, rendimiento_paquete: 100, costo_unitario_usd: 0.02, alerta_stock_minimo: 30 },
  { id: 11, nombre: 'Refresco Glu 400ml', unidad: 'und', costo_paquete_usd: 5.90, rendimiento_paquete: 15, costo_unitario_usd: 0.3933, alerta_stock_minimo: 12 },
  { id: 12, nombre: 'Refresco 1L', unidad: 'und', costo_paquete_usd: 5.00, rendimiento_paquete: 6, costo_unitario_usd: 0.8333, alerta_stock_minimo: 8 },
];

const SEDES_SEMILLA = [
  { id: 1, nombre: 'Almacén Central', es_almacen: true, activa: true },
  { id: 2, nombre: 'Sede 1 (Puesto Plaza)', es_almacen: false, activa: true },
  { id: 3, nombre: 'Sede 2 (Puesto Boulevard)', es_almacen: false, activa: true },
];

const USUARIOS_SEMILLA = [];

const MODIFICADORES_RAPIDOS = [
  'Sin papitas', 'Sin mostaza', 'Sin cebolla', 'Sin salsa de tomate',
  'Sin mayonesa', 'Sin verduras', 'Extra salsa', 'Para llevar'
];

const PRODUCTOS_SEMILLA = [
  {
    id: 1,
    nombre: 'Perro Caliente Sencillo',
    tipo: 'INDIVIDUAL',
    precio_venta_usd: 1.50,
    activo: true,
    receta: [
      { insumo_id: 1, cantidad: 1 },
      { insumo_id: 3, cantidad: 1 },
      { insumo_id: 4, cantidad: 1 },
      { insumo_id: 5, cantidad: 1 },
      { insumo_id: 6, cantidad: 1 },
      { insumo_id: 7, cantidad: 1 },
      { insumo_id: 8, cantidad: 1 },
      { insumo_id: 9, cantidad: 1 },
      { insumo_id: 10, cantidad: 1 }
    ]
  },
  {
    id: 2,
    nombre: 'Perro Grande Yumpi Especial',
    tipo: 'INDIVIDUAL',
    precio_venta_usd: 2.00,
    activo: true,
    receta: [
      { insumo_id: 2, cantidad: 1 },
      { insumo_id: 3, cantidad: 1 },
      { insumo_id: 4, cantidad: 1 },
      { insumo_id: 5, cantidad: 1 },
      { insumo_id: 6, cantidad: 1 },
      { insumo_id: 7, cantidad: 1 },
      { insumo_id: 8, cantidad: 1 },
      { insumo_id: 9, cantidad: 1 },
      { insumo_id: 10, cantidad: 1 }
    ]
  },
  {
    id: 3,
    nombre: 'Combo Dúo Clásico (2 Peq + 1 Glu)',
    tipo: 'COMBO',
    precio_venta_usd: 3.00,
    activo: true,
    receta: [
      { insumo_id: 1, cantidad: 2 },
      { insumo_id: 3, cantidad: 2 },
      { insumo_id: 4, cantidad: 2 },
      { insumo_id: 5, cantidad: 2 },
      { insumo_id: 6, cantidad: 2 },
      { insumo_id: 7, cantidad: 2 },
      { insumo_id: 8, cantidad: 2 },
      { insumo_id: 9, cantidad: 2 },
      { insumo_id: 10, cantidad: 2 },
      { insumo_id: 11, cantidad: 1 }
    ]
  },
  {
    id: 4,
    nombre: 'Combo Familiar (4 Peq + Refresco 1L)',
    tipo: 'COMBO',
    precio_venta_usd: 6.00,
    activo: true,
    receta: [
      { insumo_id: 1, cantidad: 4 },
      { insumo_id: 3, cantidad: 4 },
      { insumo_id: 4, cantidad: 4 },
      { insumo_id: 5, cantidad: 4 },
      { insumo_id: 6, cantidad: 4 },
      { insumo_id: 7, cantidad: 4 },
      { insumo_id: 8, cantidad: 4 },
      { insumo_id: 9, cantidad: 4 },
      { insumo_id: 10, cantidad: 4 },
      { insumo_id: 12, cantidad: 1 }
    ]
  },
  {
    id: 5,
    nombre: 'Refresco Glu 400ml',
    tipo: 'BEBIDA',
    precio_venta_usd: 1.00,
    activo: true,
    receta: [{ insumo_id: 11, cantidad: 1 }]
  },
  {
    id: 6,
    nombre: 'Refresco 1L',
    tipo: 'BEBIDA',
    precio_venta_usd: 2.00,
    activo: true,
    receta: [{ insumo_id: 12, cantidad: 1 }]
  },
  {
    id: 7,
    nombre: 'Extra Salchicha',
    tipo: 'EXTRA',
    precio_venta_usd: 0.80,
    activo: true,
    receta: [{ insumo_id: 3, cantidad: 1 }]
  },
  {
    id: 8,
    nombre: 'Extra Papitas Crujientes',
    tipo: 'EXTRA',
    precio_venta_usd: 0.50,
    activo: true,
    receta: [{ insumo_id: 9, cantidad: 1 }]
  }
];

// Generador de órdenes semilla históricas para poblar los reportes
// Sin datos ficticios - Modo Producción
function generarOrdenesHistoricas() { return []; }

function AppInner({ usuarioInicial, onLogout }) {
  // 1. Estados base que vienen desde Supabase
  const [sedes, setSedes] = useState(SEDES_SEMILLA);
  const [usuariosSistema, setUsuariosSistema] = useState(USUARIOS_SEMILLA);
  const [metodosQR, setMetodosQR] = useState([{ id: 1, titulo: 'Pago Móvil Principal', banco: 'Banesco (0134)', telefono: '0424-5371079', rif_cedula: 'V-19000000', qr_base64: '' }]);
  const [motorBD, setMotorBD] = useState('Sincronizando...');
  const [mostrarQRModal, setMostrarQRModal] = useState(false);

  // 2. Estado de sesión y autenticación
  const [usuarioActual, setUsuarioActual] = useState(usuarioInicial);
  const [sedeActivaId, setSedeActivaId] = useState(usuarioInicial.sede_id || 2);
  const [vistaActual, setVistaActual] = useState('pos');
  const [tabCocina, setTabCocina] = useState('preparando');

  // 3. Configuración de Tasas y Finanzas
  const [tasaBCV, setTasaBCV] = useState(45.50);
  const [tasaManual, setTasaManual] = useState(46.00);
  const [tasaReposicion, setTasaReposicion] = useState(47.50);
  const [usarTasaManual, setUsarTasaManual] = useState(false);
  const [pctReservaOperativa, setPctReservaOperativa] = useState(10.0);
  const [gastosOperativosUSD, setGastosOperativosUSD] = useState(0.0);

  // 4. Inventario por Sede
  const [inventarioSedes, setInventarioSedes] = useState(() => {
    const inv = {};
    [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12].forEach(i => {
      inv[`2_${i}`] = i === 1 ? 40 : (i === 2 ? 20 : (i === 3 ? 50 : (i === 11 ? 25 : (i === 12 ? 15 : 200))));
      inv[`3_${i}`] = i === 1 ? 30 : (i === 2 ? 15 : (i === 3 ? 35 : (i === 11 ? 20 : (i === 12 ? 10 : 150))));
      inv[`1_${i}`] = 500;
    });
    return inv;
  });

  // 5. Insumos, Productos y Órdenes
  const [insumos, setInsumos] = useState(INSUMOS_SEMILLA);
  const [productos, setProductos] = useState(PRODUCTOS_SEMILLA);
  const [ordenes, setOrdenes] = useState([]);
  const [auditoriaLogs, setAuditoriaLogs] = useState([]);

  const cargarDesdeBackend = async () => {
    try {
      const res = await fetch('/api/init');
      const data = await res.json();
      if (data && data.success) {
        setMotorBD(data.motor_bd || 'Conectado');
        if (data.config) {
          setTasaBCV(data.config.tasa_bcv);
          setTasaManual(data.config.tasa_manual);
          setTasaReposicion(data.config.tasa_reposicion);
          setUsarTasaManual(data.config.usar_tasa_manual);
          setPctReservaOperativa(data.config.pct_reserva_operativa);
        }
        if (data.sedes && data.sedes.length > 0) setSedes(data.sedes);
        if (data.usuarios && data.usuarios.length > 0) setUsuariosSistema(data.usuarios);
        if (data.insumos && data.insumos.length > 0) setInsumos(data.insumos);
        if (data.productos && data.productos.length > 0) setProductos(data.productos);
        if (data.inventario_sedes && Object.keys(data.inventario_sedes).length > 0) setInventarioSedes(data.inventario_sedes);
        if (data.metodos_qr && data.metodos_qr.length > 0) setMetodosQR(data.metodos_qr);
        if (data.ordenes && data.ordenes.length > 0) setOrdenes(data.ordenes);
      }
    } catch (e) {
      console.warn('Operando en caché local:', e);
    }
  };

  useEffect(() => {
    cargarDesdeBackend();
  }, []);


  // Carrito de POS
  const [carrito, setCarrito] = useState([]);
  const [filtroCategoria, setFiltroCategoria] = useState('TODOS');

  // Modales
  const [modalCobroAbierto, setModalCobroAbierto] = useState(false);
  const [modalLoginAbierto, setModalLoginAbierto] = useState(false);
  const [modalMermaAbierto, setModalMermaAbierto] = useState(false);
  const [modalAuditoriaAbierto, setModalAuditoriaAbierto] = useState(false);
  const [modalReportePrintAbierto, setModalReportePrintAbierto] = useState(false);
  const [ordenParaAuditar, setOrdenParaAuditar] = useState(null);
  const [justificacionAuditoria, setJustificacionAuditoria] = useState('');

  // Variables de Cobro Inteligente
  const [cobroRecibidoUSD, setCobroRecibidoUSD] = useState(0);
  const [cobroRecibidoBS, setCobroRecibidoBS] = useState(0);
  const [cobroVueltoUSD, setCobroVueltoUSD] = useState(0);
  const [cobroVueltoBS, setCobroVueltoBS] = useState(0);
  const [clienteNombre, setClienteNombre] = useState('');
  const [clienteTelefono, setClienteTelefono] = useState('');
  const [esFiado, setEsFiado] = useState(false);
  const [notasCocina, setNotasCocina] = useState('');
  const [modsSeleccionados, setModsSeleccionados] = useState([]);
  const [modPersonalizado, setModPersonalizado] = useState('');

  // Inventario (entradas, ajustes, transferencias, insumos)
  const [sedeStockSel, setSedeStockSel] = useState(null);
  const [modalMovAbierto, setModalMovAbierto] = useState(false);
  const [movForm, setMovForm] = useState({ tipo: 'COMPRA', insumo_id: '', cantidad: '', unidadEntrada: 'unidad', sede_destino_id: '', motivo: '', costo_paquete_usd: '', nuevo_stock: '' });
  const [modalInsumoAbierto, setModalInsumoAbierto] = useState(false);
  const [insumoForm, setInsumoForm] = useState(null);
  const [guardando, setGuardando] = useState(false);

  // Mi cuenta / PIN
  const [pinActual, setPinActual] = useState('');
  const [pinNuevo, setPinNuevo] = useState('');
  const [pinRepetir, setPinRepetir] = useState('');
  const [pinResetUser, setPinResetUser] = useState('');
  const [pinResetNuevo, setPinResetNuevo] = useState('');
  const [pinMsg, setPinMsg] = useState('');
  const [copiado, setCopiado] = useState(null);

  // ==========================================
  // ESTADO DEL MÓDULO DE REPORTES AVANZADOS
  // ==========================================
  const hoyStr = new Date().toISOString().split('T')[0];
  const hace7DiasStr = new Date(Date.now() - 7 * 86400000).toISOString().split('T')[0];

  const [reporteSubTab, setReporteSubTab] = useState('ventas');
  const [filtroPresetFecha, setFiltroPresetFecha] = useState('7dias');
  const [fechaInicioReporte, setFechaInicioReporte] = useState(hace7DiasStr);
  const [fechaFinReporte, setFechaFinReporte] = useState(hoyStr);
  const [filtroSedeReporte, setFiltroSedeReporte] = useState('todas');

  const tasaActiva = usarTasaManual ? tasaManual : tasaBCV;
  const sedeActual = useMemo(() => sedes.find(s => s.id === sedeActivaId) || sedes[1], [sedes, sedeActivaId]);

  // Sede cuyo inventario se está viendo (el superusuario puede mirar también el Almacén)
  const sedeStockId = usuarioActual.rol === 'superusuario' ? (sedeStockSel ?? sedeActivaId) : sedeActivaId;
  const sedeStockActual = sedes.find(s => s.id === sedeStockId);
  const insumoMov = insumos.find(i => Number(i.id) === Number(movForm.insumo_id));

  // Actualizar presets de fecha
  useEffect(() => {
    const ahora = new Date();
    const hoy = ahora.toISOString().split('T')[0];
    if (filtroPresetFecha === 'hoy') {
      setFechaInicioReporte(hoy);
      setFechaFinReporte(hoy);
    } else if (filtroPresetFecha === 'ayer') {
      const ayer = new Date(Date.now() - 86400000).toISOString().split('T')[0];
      setFechaInicioReporte(ayer);
      setFechaFinReporte(ayer);
    } else if (filtroPresetFecha === '7dias') {
      const d7 = new Date(Date.now() - 7 * 86400000).toISOString().split('T')[0];
      setFechaInicioReporte(d7);
      setFechaFinReporte(hoy);
    } else if (filtroPresetFecha === '30dias') {
      const d30 = new Date(Date.now() - 30 * 86400000).toISOString().split('T')[0];
      setFechaInicioReporte(d30);
      setFechaFinReporte(hoy);
    }
  }, [filtroPresetFecha]);

  // Si el usuario es cajero, está estrictamente bloqueado a su sede asignada
  useEffect(() => {
    if (usuarioActual.rol === 'cajero' && usuarioActual.sede_id) {
      setSedeActivaId(usuarioActual.sede_id);
    }
  }, [usuarioActual]);

  // Totales de Carrito
  const totalCarritoUSD = useMemo(() => {
    return carrito.reduce((acc, item) => acc + item.producto.precio_venta_usd * item.cantidad, 0);
  }, [carrito]);

  const totalCarritoBS = useMemo(() => {
    return totalCarritoUSD * tasaActiva;
  }, [totalCarritoUSD, tasaActiva]);

  // Calcular Costo BOM Congelado
  const calcularCostoBOM = (items) => {
    let costoTotal = 0;
    const insumoCostos = new Map(insumos.map(i => [i.id, i.costo_unitario_usd]));
    items.forEach(item => {
      item.producto.receta.forEach(r => {
        const costoUnit = insumoCostos.get(r.insumo_id) || 0;
        costoTotal += costoUnit * r.cantidad * item.cantidad;
      });
    });
    return costoTotal;
  };

  const claveMods = (mods) => [...mods].sort().join('|');

  const agregarAlCarrito = (prod, mods = []) => {
    const lista = Array.isArray(mods) ? mods : (mods ? [mods] : []);
    setCarrito(prev => {
      const idx = prev.findIndex(item => item.producto.id === prod.id && claveMods(item.modificadores) === claveMods(lista));
      if (idx > -1) {
        return prev.map((item, i) => (i === idx ? { ...item, cantidad: item.cantidad + 1 } : item));
      }
      return [...prev, { producto: prod, cantidad: 1, modificadores: [...lista] }];
    });
    setModsSeleccionados([]);
    setModPersonalizado('');
  };

  const cambiarCantidad = (idx, delta) => {
    setCarrito(prev => prev.flatMap((item, i) => {
      if (i !== idx) return [item];
      const nueva = item.cantidad + delta;
      return nueva > 0 ? [{ ...item, cantidad: nueva }] : [];
    }));
  };

  const quitarDelCarrito = (idx) => setCarrito(prev => prev.filter((_, i) => i !== idx));

  const alternarMod = (mod) => {
    setModsSeleccionados(prev => (prev.includes(mod) ? prev.filter(m => m !== mod) : [...prev, mod]));
  };

  const agregarModPersonalizado = () => {
    const t = modPersonalizado.trim().slice(0, 40);
    if (!t) return;
    setModsSeleccionados(prev => (prev.includes(t) ? prev : [...prev, t]));
    setModPersonalizado('');
  };

  const netoPagadoUSD = useMemo(() => {
    return (
      cobroRecibidoUSD +
      (cobroRecibidoBS > 0 ? cobroRecibidoBS / tasaActiva : 0) -
      cobroVueltoUSD -
      (cobroVueltoBS > 0 ? cobroVueltoBS / tasaActiva : 0)
    );
  }, [cobroRecibidoUSD, cobroRecibidoBS, cobroVueltoUSD, cobroVueltoBS, tasaActiva]);

  const diferenciaPago = useMemo(() => {
    return netoPagadoUSD - totalCarritoUSD;
  }, [netoPagadoUSD, totalCarritoUSD]);

  const confirmarVenta = () => {
    if (carrito.length === 0) return;
    if (!esFiado && diferenciaPago < -0.05) {
      alert("El pago aún no está cuadrado. Falta cubrir el total de la orden.");
      return;
    }

    const costoInsumos = calcularCostoBOM(carrito);
    const nuevoInventario = { ...inventarioSedes };
    carrito.forEach(cartItem => {
      cartItem.producto.receta.forEach(rec => {
        const key = `${sedeActivaId}_${rec.insumo_id}`;
        const stockActual = nuevoInventario[key] ?? 0;
        nuevoInventario[key] = Math.max(0, stockActual - rec.cantidad * cartItem.cantidad);
      });
    });
    setInventarioSedes(nuevoInventario);

    const ahora = new Date();
    const nuevaOrden = {
      id: Date.now(),
      sede_id: sedeActivaId,
      cajero_id: usuarioActual.id,
      cajero_nombre: usuarioActual.nombre,
      cliente_nombre: clienteNombre.trim() || (esFiado ? 'Cliente Fiado' : 'Consumidor'),
      cliente_telefono: clienteTelefono.trim(),
      total_venta_usd: totalCarritoUSD,
      costo_insumos_usd: costoInsumos,
      tasa_aplicada: tasaActiva,
      tasa_reposicion_momento: tasaReposicion,
      recibido_usd: cobroRecibidoUSD,
      recibido_bs: cobroRecibidoBS,
      vuelto_usd: cobroVueltoUSD,
      vuelto_bs: cobroVueltoBS,
      estado_pago: esFiado ? 'FIADO' : 'PAGADA',
      estado_cocina: 'PREPARANDO',
      notas_cocina: notasCocina,
      items: carrito.map(c => ({
        producto_id: c.producto.id,
        nombre: c.producto.nombre,
        cantidad: c.cantidad,
        precio_unitario_usd: c.producto.precio_venta_usd,
        costo_unitario_usd: calcularCostoBOM([{ producto: c.producto, cantidad: 1 }]),
        modificadores: c.modificadores
      })),
      created_at: ahora.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
      fecha_corta: ahora.toISOString().split('T')[0]
    };

    setOrdenes(prev => [nuevaOrden, ...prev]);
    fetch('/api/orden/crear', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        sede_id: sedeActivaId,
        cajero_id: usuarioActual.id,
        cliente_nombre: nuevaOrden.cliente_nombre,
        cliente_telefono: nuevaOrden.cliente_telefono,
        recibido_usd: cobroRecibidoUSD,
        recibido_bs: cobroRecibidoBS,
        vuelto_usd: cobroVueltoUSD,
        vuelto_bs: cobroVueltoBS,
        estado_pago: nuevaOrden.estado_pago,
        notas_cocina: notasCocina,
        items: carrito.map(c => ({ producto_id: c.producto.id, cantidad: c.cantidad, modificadores: c.modificadores }))
      })
    }).then(r => r.json()).then(d => {
      if (d && d.success === false) alert('No se pudo guardar la orden: ' + (d.error || 'error desconocido'));
      cargarDesdeBackend();
    }).catch(err => console.warn('Guardado en cola local:', err));
    setCarrito([]);
    setModalCobroAbierto(false);
    setCobroRecibidoUSD(0);
    setCobroRecibidoBS(0);
    setCobroVueltoUSD(0);
    setCobroVueltoBS(0);
    setClienteNombre('');
    setClienteTelefono('');
    setNotasCocina('');
    setEsFiado(false);
  };

  // ---------- Mi cuenta / PIN ----------
  const cambiarMiPin = async () => {
    if (pinNuevo !== pinRepetir) { setPinMsg('Los PIN nuevos no coinciden.'); return; }
    try {
      const res = await fetch('/api/auth/cambiar-pin', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ pin_actual: pinActual, pin_nuevo: pinNuevo })
      });
      const data = await res.json();
      setPinMsg(data.success ? 'PIN actualizado.' : (data.error || 'No se pudo cambiar el PIN.'));
      if (data.success) { setPinActual(''); setPinNuevo(''); setPinRepetir(''); }
    } catch (e) { setPinMsg('Sin conexión con el servidor.'); }
  };

  const restablecerPin = async () => {
    if (!pinResetUser) { setPinMsg('Elige un usuario.'); return; }
    try {
      const res = await fetch('/api/auth/cambiar-pin', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_id: Number(pinResetUser), pin_nuevo: pinResetNuevo })
      });
      const data = await res.json();
      setPinMsg(data.success ? 'PIN restablecido.' : (data.error || 'No se pudo restablecer el PIN.'));
      if (data.success) setPinResetNuevo('');
    } catch (e) { setPinMsg('Sin conexión con el servidor.'); }
  };

  // ---------- Inventario ----------
  const abrirMovimiento = (tipo, insumoId = '') => {
    setMovForm({
      tipo,
      insumo_id: insumoId || (insumos[0] ? insumos[0].id : ''),
      cantidad: '', unidadEntrada: 'unidad', sede_destino_id: '', motivo: '', costo_paquete_usd: '', nuevo_stock: ''
    });
    setModalMovAbierto(true);
  };

  const enviarMovimiento = async () => {
    if (!insumoMov) { alert('Elige un insumo.'); return; }
    const body = { sede_id: sedeStockId, insumo_id: insumoMov.id, tipo: movForm.tipo, motivo: movForm.motivo };
    if (movForm.tipo === 'AJUSTE') {
      if (movForm.nuevo_stock === '' || Number(movForm.nuevo_stock) < 0) { alert('Escribe el conteo real (0 o más).'); return; }
      if (!movForm.motivo.trim()) { alert('Escribe el motivo del ajuste.'); return; }
      body.nuevo_stock = Number(movForm.nuevo_stock);
    } else {
      let cant = Number(movForm.cantidad);
      if (!(cant > 0)) { alert('Escribe una cantidad mayor a 0.'); return; }
      if (movForm.unidadEntrada === 'paquete') cant = cant * insumoMov.rendimiento_paquete;
      body.cantidad = cant;
      if (movForm.tipo === 'TRANSFERENCIA') {
        if (!movForm.sede_destino_id) { alert('Elige la sede destino.'); return; }
        body.sede_destino_id = Number(movForm.sede_destino_id);
      }
      if (movForm.tipo === 'COMPRA' && movForm.costo_paquete_usd !== '') {
        body.costo_paquete_usd = Number(movForm.costo_paquete_usd);
      }
    }
    setGuardando(true);
    try {
      const res = await fetch('/api/inventario/movimiento', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      });
      const data = await res.json();
      if (!data.success) {
        alert(data.error || 'No se pudo registrar el movimiento.');
      } else {
        setModalMovAbierto(false);
        await cargarDesdeBackend();
      }
    } catch (e) { alert('Sin conexión con el servidor.'); }
    setGuardando(false);
  };

  const abrirInsumo = (ins = null) => {
    setInsumoForm(ins
      ? { id: ins.id, nombre: ins.nombre, unidad: ins.unidad, costo_paquete_usd: String(ins.costo_paquete_usd), rendimiento_paquete: String(ins.rendimiento_paquete), alerta_stock_minimo: String(ins.alerta_stock_minimo), stock_inicial: '' }
      : { id: null, nombre: '', unidad: 'und', costo_paquete_usd: '', rendimiento_paquete: '', alerta_stock_minimo: '10', stock_inicial: '' });
    setModalInsumoAbierto(true);
  };

  const guardarInsumo = async () => {
    const f = insumoForm;
    if (!f.nombre.trim()) { alert('Escribe el nombre del insumo.'); return; }
    if (!(Number(f.costo_paquete_usd) >= 0) || f.costo_paquete_usd === '') { alert('Escribe el costo del paquete.'); return; }
    if (!(Number(f.rendimiento_paquete) > 0)) { alert('El rendimiento por paquete debe ser mayor a 0.'); return; }
    setGuardando(true);
    try {
      const res = await fetch('/api/admin/crud', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          entidad: 'insumos',
          accion: f.id ? 'UPDATE' : 'CREATE',
          registro: {
            id: f.id, nombre: f.nombre, unidad: f.unidad,
            costo_paquete_usd: Number(f.costo_paquete_usd),
            rendimiento_paquete: Number(f.rendimiento_paquete),
            alerta_stock_minimo: Number(f.alerta_stock_minimo || 0),
            stock_inicial: Number(f.stock_inicial || 0),
            sede_id: sedeStockId
          }
        })
      });
      const data = await res.json();
      if (!data.success) {
        alert(data.error || 'No se pudo guardar el insumo.');
      } else {
        setModalInsumoAbierto(false);
        await cargarDesdeBackend();
      }
    } catch (e) { alert('Sin conexión con el servidor.'); }
    setGuardando(false);
  };

  const anularOrdenAuditoria = () => {
    if (!ordenParaAuditar) return;
    if (!justificacionAuditoria.trim()) {
      alert("La justificación de auditoría es obligatoria para garantizar la trazabilidad.");
      return;
    }

    const nuevoInventario = { ...inventarioSedes };
    ordenParaAuditar.items.forEach(item => {
      const prod = productos.find(p => p.id === item.producto_id);
      if (prod) {
        prod.receta.forEach(rec => {
          const key = `${ordenParaAuditar.sede_id}_${rec.insumo_id}`;
          nuevoInventario[key] = (nuevoInventario[key] ?? 0) + rec.cantidad * item.cantidad;
        });
      }
    });
    setInventarioSedes(nuevoInventario);

    setOrdenes(prev =>
      prev.map(o => (o.id === ordenParaAuditar.id ? { ...o, estado_pago: 'ANULADA' } : o))
    );

    const log = {
      id: Date.now(),
      superusuario_id: usuarioActual.id,
      sede_id: ordenParaAuditar.sede_id,
      accion: 'ANULAR_ORDEN',
      entidad_afectada: 'ordenes',
      registro_id: ordenParaAuditar.id,
      snapshot_anterior: { ...ordenParaAuditar },
      snapshot_nuevo: { estado_pago: 'ANULADA' },
      justificacion: justificacionAuditoria.trim(),
      created_at: new Date().toLocaleString()
    };
    setAuditoriaLogs(prev => [log, ...prev]);
    fetch('/api/auditoria/corregir', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        orden_id: ordenParaAuditar.id,
        justificacion: justificacionAuditoria.trim(),
        superusuario_id: usuarioActual.id
      })
    }).then(() => cargarDesdeBackend()).catch(err => console.warn(err));

    setModalAuditoriaAbierto(false);
    setOrdenParaAuditar(null);
    setJustificacionAuditoria('');
    alert(`Orden #${ordenParaAuditar.id} anulada correctamente. El stock de la Sede fue restablecido.`);
  };

  // ==============================================================
  // MOTOR DE CÁLCULO ANALÍTICO PARA REPORTES (FILTRADO POR FECHA)
  // ==============================================================
  const ordenesFiltradasReporte = useMemo(() => {
    return ordenes.filter(o => {
      const cumpleSede = filtroSedeReporte === 'todas' || o.sede_id === filtroSedeReporte;
      const cumpleFecha = o.fecha_corta >= fechaInicioReporte && o.fecha_corta <= fechaFinReporte;
      return cumpleSede && cumpleFecha;
    });
  }, [ordenes, filtroSedeReporte, fechaInicioReporte, fechaFinReporte]);

  // Reporte 1: Ventas
  const reporte1_Ventas = useMemo(() => {
    const validas = ordenesFiltradasReporte.filter(o => o.estado_pago === 'PAGADA');
    const totalUSD = validas.reduce((acc, o) => acc + o.total_venta_usd, 0);
    const totalCostosBOM = validas.reduce((acc, o) => acc + o.costo_insumos_usd, 0);
    const cantOrdenes = validas.length;
    const ticketPromedio = cantOrdenes > 0 ? totalUSD / cantOrdenes : 0;

    const totalRecibidoUSD = validas.reduce((acc, o) => acc + o.recibido_usd, 0);
    const totalRecibidoBS = validas.reduce((acc, o) => acc + o.recibido_bs, 0);
    const totalVueltoUSD = validas.reduce((acc, o) => acc + o.vuelto_usd, 0);
    const totalVueltoBS = validas.reduce((acc, o) => acc + o.vuelto_bs, 0);

    // Agrupación por día para gráfica
    const porDia = {};
    validas.forEach(o => {
      const f = o.fecha_corta;
      if (!porDia[f]) porDia[f] = { fecha: f, totalUSD: 0, ordenes: 0 };
      porDia[f].totalUSD += o.total_venta_usd;
      porDia[f].ordenes += 1;
    });

    const listaDias = Object.values(porDia).sort((a, b) => a.fecha.localeCompare(b.fecha));

    return {
      totalUSD,
      totalCostosBOM,
      cantOrdenes,
      ticketPromedio,
      totalRecibidoUSD,
      totalRecibidoBS,
      totalVueltoUSD,
      totalVueltoBS,
      listaDias,
      ordenes: validas
    };
  }, [ordenesFiltradasReporte]);

  // Reporte 2: Rotación de Inventario (BOM consumido + Mermas)
  const reporte2_Inventario = useMemo(() => {
    const validas = ordenesFiltradasReporte.filter(o => o.estado_pago === 'PAGADA');
    const consumosPorInsumo = {};

    validas.forEach(o => {
      o.items.forEach(it => {
        const prod = productos.find(p => p.id === it.producto_id);
        if (prod) {
          prod.receta.forEach(r => {
            consumosPorInsumo[r.insumo_id] = (consumosPorInsumo[r.insumo_id] || 0) + r.cantidad * it.cantidad;
          });
        }
      });
    });

    const tablaInsumos = insumos.map(ins => {
      const consumo = consumosPorInsumo[ins.id] || 0;
      // Stock total según sedes filtradas
      let stockActual = 0;
      if (filtroSedeReporte === 'todas') {
        stockActual = (inventarioSedes[`2_${ins.id}`] || 0) + (inventarioSedes[`3_${ins.id}`] || 0);
      } else {
        stockActual = inventarioSedes[`${filtroSedeReporte}_${ins.id}`] || 0;
      }

      const valorConsumidoUSD = consumo * ins.costo_unitario_usd;
      const ratioRotacion = stockActual > 0 ? (consumo / stockActual) : (consumo > 0 ? 99 : 0);
      const diasRestantes = consumo > 0 ? Math.round((stockActual / (consumo / Math.max(1, reporte1_Ventas.listaDias.length || 1)))) : 999;

      let velocidad = 'ESTABLE';
      if (ratioRotacion >= 0.5) velocidad = 'ALTA';
      else if (ratioRotacion >= 0.2) velocidad = 'MEDIA';

      return {
        ...ins,
        consumo,
        stockActual,
        valorConsumidoUSD,
        ratioRotacion,
        diasRestantes,
        velocidad
      };
    });

    const totalValorConsumido = tablaInsumos.reduce((acc, i) => acc + i.valorConsumidoUSD, 0);

    return {
      tablaInsumos,
      totalValorConsumido
    };
  }, [ordenesFiltradasReporte, insumos, productos, inventarioSedes, filtroSedeReporte, reporte1_Ventas.listaDias.length]);

  // Reporte 3: Rentabilidad por Producto y Sede
  const reporte3_Rentabilidad = useMemo(() => {
    const validas = ordenesFiltradasReporte.filter(o => o.estado_pago === 'PAGADA');
    const statsProd = {};

    productos.forEach(p => {
      statsProd[p.id] = { producto: p, cant: 0, ingresosUSD: 0, costoBOM: 0 };
    });

    validas.forEach(o => {
      o.items.forEach(it => {
        if (statsProd[it.producto_id]) {
          statsProd[it.producto_id].cant += it.cantidad;
          statsProd[it.producto_id].ingresosUSD += it.precio_unitario_usd * it.cantidad;
          statsProd[it.producto_id].costoBOM += it.costo_unitario_usd * it.cantidad;
        }
      });
    });

    const tablaProductos = Object.values(statsProd).map(s => {
      const gananciaBruta = s.ingresosUSD - s.costoBOM;
      const margenPct = s.ingresosUSD > 0 ? (gananciaBruta / s.ingresosUSD) * 100 : 0;
      return {
        ...s,
        gananciaBruta,
        margenPct
      };
    }).sort((a, b) => b.ingresosUSD - a.ingresosUSD);

    // Comparativa por Sede
    const sedesAnalizadas = [2, 3].map(sId => {
      const ordsSede = validas.filter(o => o.sede_id === sId);
      const sTotalUSD = ordsSede.reduce((acc, o) => acc + o.total_venta_usd, 0);
      const sCostoBOM = ordsSede.reduce((acc, o) => acc + o.costo_insumos_usd, 0);
      const sGanancia = sTotalUSD - sCostoBOM;
      const sMargen = sTotalUSD > 0 ? (sGanancia / sTotalUSD) * 100 : 0;
      const sedeObj = sedes.find(s => s.id === sId);
      return {
        sede_id: sId,
        nombre: sedeObj ? sedeObj.nombre : `Sede ${sId}`,
        totalUSD: sTotalUSD,
        costoBOM: sCostoBOM,
        gananciaBruta: sGanancia,
        margenPct: sMargen,
        ordenes: ordsSede.length
      };
    });

    const totalIngresos = tablaProductos.reduce((acc, p) => acc + p.ingresosUSD, 0);
    const totalCostos = tablaProductos.reduce((acc, p) => acc + p.costoBOM, 0);
    const margenPromedioGlobal = totalIngresos > 0 ? ((totalIngresos - totalCostos) / totalIngresos) * 100 : 0;

    return {
      tablaProductos,
      sedesAnalizadas,
      totalIngresos,
      totalCostos,
      margenPromedioGlobal
    };
  }, [ordenesFiltradasReporte, productos, sedes]);

  // Reporte 4: Desempeño de Cajeros
  const reporte4_Cajeros = useMemo(() => {
    const validas = ordenesFiltradasReporte.filter(o => o.estado_pago === 'PAGADA');
    const cajerosList = usuariosSistema.filter(u => u.rol === 'cajero' || u.id === 1);

    const stats = cajerosList.map(caj => {
      const ords = validas.filter(o => o.cajero_id === caj.id);
      const totalUSD = ords.reduce((acc, o) => acc + o.total_venta_usd, 0);
      const totalBS = ords.reduce((acc, o) => acc + o.total_venta_usd * o.tasa_aplicada, 0);
      const cant = ords.length;
      const ticketPromedio = cant > 0 ? totalUSD / cant : 0;
      const sedeAsignada = sedes.find(s => s.id === caj.sede_id)?.nombre || 'Todas (Dueño)';

      return {
        ...caj,
        sedeNombre: sedeAsignada,
        ordenesProcesadas: cant,
        totalUSD,
        totalBS,
        ticketPromedio,
        diferenciaArqueoUSD: 0 // Arqueo cuadrado
      };
    });

    return stats;
  }, [ordenesFiltradasReporte, sedes]);

  // ==============================================================
  // GENERADORES DE EXPORTACIÓN (CSV Y PDF/PRINT)
  // ==============================================================
  const exportarCSV = () => {
    let csvContenido = '\uFEFF'; // UTF-8 BOM
    const timestamp = new Date().toISOString().replace('T', ' ').slice(0, 19);

    if (reporteSubTab === 'ventas') {
      csvContenido += `DOGGIPOS MULTISEDE - REPORTE DE RENDIMIENTO DE VENTAS\n`;
      csvContenido += `Fecha de generación: ${timestamp}\n`;
      csvContenido += `Rango evaluado: ${fechaInicioReporte} al ${fechaFinReporte}\n`;
      csvContenido += `Sede: ${filtroSedeReporte === 'todas' ? 'Todas las Sedes' : `Sede ${filtroSedeReporte}`}\n\n`;
      csvContenido += `RESUMEN EJECUTIVO\n`;
      csvContenido += `Total Facturado USD,${reporte1_Ventas.totalUSD.toFixed(2)}\n`;
      csvContenido += `Total Costo BOM USD,${reporte1_Ventas.totalCostosBOM.toFixed(2)}\n`;
      csvContenido += `Total Órdenes,${reporte1_Ventas.cantOrdenes}\n`;
      csvContenido += `Ticket Promedio USD,${reporte1_Ventas.ticketPromedio.toFixed(2)}\n`;
      csvContenido += `Total Recibido Efectivo USD,${reporte1_Ventas.totalRecibidoUSD.toFixed(2)}\n`;
      csvContenido += `Total Recibido Pago Móvil Bs,${reporte1_Ventas.totalRecibidoBS.toFixed(2)}\n\n`;
      csvContenido += `DETALLE DE ÓRDENES\n`;
      csvContenido += `ID Orden,Fecha,Hora,Sede,Cajero,Cliente,Total USD,Costo Insumos USD,Recibido USD,Recibido BS,Vuelto USD,Vuelto BS\n`;

      reporte1_Ventas.ordenes.forEach(o => {
        csvContenido += `${o.id},${o.fecha_corta},"${o.created_at}",Sede ${o.sede_id},"${o.cajero_nombre}","${o.cliente_nombre}",${o.total_venta_usd.toFixed(2)},${o.costo_insumos_usd.toFixed(2)},${o.recibido_usd.toFixed(2)},${o.recibido_bs.toFixed(2)},${o.vuelto_usd.toFixed(2)},${o.vuelto_bs.toFixed(2)}\n`;
      });
    } else if (reporteSubTab === 'inventario') {
      csvContenido += `DOGGIPOS MULTISEDE - REPORTE DE ROTACIÓN DE INVENTARIO\n`;
      csvContenido += `Rango evaluado: ${fechaInicioReporte} al ${fechaFinReporte}\n\n`;
      csvContenido += `ID Insumo,Nombre,Unidad,Consumo BOM,Stock Actual,Costo Unit USD,Valor Consumido USD,Velocidad Rotación\n`;

      reporte2_Inventario.tablaInsumos.forEach(i => {
        csvContenido += `${i.id},"${i.nombre}",${i.unidad},${i.consumo},${i.stockActual},${i.costo_unitario_usd.toFixed(4)},${i.valorConsumidoUSD.toFixed(2)},${i.velocidad}\n`;
      });
    } else if (reporteSubTab === 'rentabilidad') {
      csvContenido += `DOGGIPOS MULTISEDE - REPORTE DE RENTABILIDAD POR PRODUCTO Y UBICACIÓN\n`;
      csvContenido += `Rango evaluado: ${fechaInicioReporte} al ${fechaFinReporte}\n\n`;
      csvContenido += `Producto,Tipo,Unidades Vendidas,Ingresos Brutos USD,Costo BOM USD,Ganancia Bruta USD,Margen %\n`;

      reporte3_Rentabilidad.tablaProductos.forEach(p => {
        csvContenido += `"${p.producto.nombre}",${p.producto.tipo},${p.cant},${p.ingresosUSD.toFixed(2)},${p.costoBOM.toFixed(2)},${p.gananciaBruta.toFixed(2)},${p.margenPct.toFixed(1)}%\n`;
      });
    } else if (reporteSubTab === 'cajeros') {
      csvContenido += `DOGGIPOS MULTISEDE - REPORTE DE DESEMPEÑO DE CAJEROS\n`;
      csvContenido += `Rango evaluado: ${fechaInicioReporte} al ${fechaFinReporte}\n\n`;
      csvContenido += `Cajero,Sede Asignada,Órdenes Procesadas,Total Facturado USD,Total Equivalente Bs,Ticket Promedio USD\n`;

      reporte4_Cajeros.forEach(c => {
        csvContenido += `"${c.nombre}","${c.sedeNombre}",${c.ordenesProcesadas},${c.totalUSD.toFixed(2)},${c.totalBS.toFixed(2)},${c.ticketPromedio.toFixed(2)}\n`;
      });
    }

    const blob = new Blob([csvContenido], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.setAttribute('download', `Burger Admin_Reporte_${reporteSubTab}_${fechaInicioReporte}_a_${fechaFinReporte}.csv`);
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  };

  const dispararImpresionPDF = () => {
    window.print();
  };

  return (
    <div className="min-h-screen bg-[#09090b] text-white flex flex-col font-sans antialiased pb-24 select-none">
      {/* ============================================================== */}
      {/* CABECERA PRINCIPAL SUPERIOR (ALTO CONTRASTE)                   */}
      {/* ============================================================== */}
      <header className="sticky top-0 z-40 bg-[#09090b]/95 backdrop-blur border-b border-[#222227] px-4 py-3 print:hidden">
        <div className="max-w-6xl mx-auto flex items-center justify-between">
          <div className="flex items-center space-x-3">
            <div className="w-9 h-9 rounded-xl bg-[#2563eb] flex items-center justify-center font-black text-white text-xl tracking-tighter">
              B
            </div>
            <div>
              <div className="flex items-center space-x-2">
                <span className="font-extrabold text-sm tracking-tight text-white">Burger Admin</span>
                <span className="text-[10px] bg-[#2563eb]/20 text-[#60a5fa] px-1.5 py-0.5 rounded font-bold uppercase tracking-wider">
                  BOM Fast-Food
                </span>
              </div>
              <div className="flex items-center space-x-2 text-xs text-zinc-400 mt-0.5">
                <Store className="w-3.5 h-3.5 text-zinc-400" />
                <span className="font-medium text-zinc-200">{sedeActual.nombre}</span>
                <span>•</span>
                <span className="flex items-center text-emerald-400 font-medium">
                  <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 mr-1 animate-pulse"></span>
                  Online
                </span>
              </div>
            </div>
          </div>

          {/* Selector de Tasas y Perfil */}
          <div className="flex items-center space-x-3">
            <div className="bg-[#111114] border border-[#222227] px-3 py-1.5 rounded-xl text-right flex items-center space-x-2">
              <div>
                <div className="text-[9px] text-zinc-400 font-bold uppercase tracking-wider flex items-center justify-end space-x-1">
                  <span>Tasa Cobro ({usarTasaManual ? 'Manual' : 'BCV'})</span>
                </div>
                <div className="text-xs font-mono font-black text-[#38bdf8]">
                  Bs {tasaActiva.toFixed(2)}
                </div>
              </div>
              <button
                title="Sincronizar tasa BCV oficial"
                onClick={() => {
                  fetch('/api/tasa/sync', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({}) })
                    .then(r => r.json())
                    .then(d => { if(d.tasa_bcv) setTasaBCV(d.tasa_bcv); alert("Tasa BCV sincronizada: Bs " + (d.tasa_bcv || tasaBCV).toFixed(2)); });
                }}
                className="p-1 rounded text-zinc-400 hover:text-white hover:bg-zinc-800 transition"
              >
                <RefreshCw className="w-3.5 h-3.5" />
              </button>
            </div>

            {/* Usuario Actual & Cambio de Rol */}
            <button
              onClick={() => setModalLoginAbierto(true)}
              className="bg-[#111114] border border-[#222227] hover:border-[#3b82f6] px-2.5 py-1.5 rounded-xl flex items-center space-x-2 transition"
            >
              <div className="w-6 h-6 rounded-lg bg-zinc-800 flex items-center justify-center text-xs font-bold text-zinc-200">
                {usuarioActual.rol === 'superusuario' ? 'S' : 'C'}
              </div>
              <div className="text-left hidden sm:block">
                <div className="text-xs font-bold leading-tight">{usuarioActual.nombre}</div>
                <div className="text-[10px] text-zinc-400 uppercase font-semibold">
                  {usuarioActual.rol}
                </div>
              </div>
            </button>
          </div>
        </div>
      </header>

      {/* ============================================================== */}
      {/* CONTENIDO PRINCIPAL SEGÚN VISTA ACTIVA                          */}
      {/* ============================================================== */}
      <main className="flex-1 max-w-6xl mx-auto w-full p-4 space-y-4">
        {/* VISTA 1: POS / CAJA */}
        {vistaActual === 'pos' && (
          <div className="space-y-4">
            {/* Barra superior del POS */}
            <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
              {usuarioActual.rol === 'superusuario' ? (
                <div className="bg-[#111114] border border-[#222227] p-3 rounded-2xl flex items-center justify-between">
                  <div className="text-xs text-zinc-400 flex items-center space-x-1.5">
                    <Store className="w-4 h-4 text-[#2563eb]" />
                    <span className="font-semibold text-white">Sede POS:</span>
                  </div>
                  <select
                    value={sedeActivaId}
                    onChange={e => setSedeActivaId(Number(e.target.value))}
                    className="bg-[#09090b] border border-[#222227] text-xs font-bold text-white rounded-xl px-3 py-1.5 focus:border-[#2563eb] outline-none"
                  >
                    {sedes
                      .filter(s => !s.es_almacen)
                      .map(s => (
                        <option key={s.id} value={s.id}>
                          {s.nombre}
                        </option>
                      ))}
                  </select>
                </div>
              ) : (
                <div className="bg-[#111114] border border-[#222227] p-3 rounded-2xl flex items-center space-x-2 text-xs">
                  <Lock className="w-4 h-4 text-amber-500" />
                  <span className="text-zinc-400">Puesto Bloqueado a:</span>
                  <span className="font-bold text-white">{sedeActual.nombre}</span>
                </div>
              )}

              <div className="bg-[#111114] border border-[#222227] p-3 rounded-2xl flex items-center justify-between">
                <div className="text-xs">
                  <span className="text-zinc-400 text-[10px] uppercase font-bold block">Gaveta Física USD</span>
                  <span className="text-base font-black text-emerald-400 font-mono">
                    ${ordenes.filter(o => o.sede_id === sedeActivaId && o.estado_pago === 'PAGADA').reduce((acc, o) => acc + o.recibido_usd - o.vuelto_usd, 0).toFixed(2)}
                  </span>
                </div>
                <div className="text-[10px] text-zinc-500 text-right">Efectivo en billetes</div>
              </div>

              <div className="bg-[#111114] border border-[#222227] p-3 rounded-2xl flex items-center justify-between">
                <div className="text-xs">
                  <span className="text-zinc-400 text-[10px] uppercase font-bold block">Banco Bs (Pago Móvil)</span>
                  <span className="text-base font-black text-[#60a5fa] font-mono">
                    Bs {ordenes.filter(o => o.sede_id === sedeActivaId && o.estado_pago === 'PAGADA').reduce((acc, o) => acc + o.recibido_bs - o.vuelto_bs, 0).toFixed(2)}
                  </span>
                </div>
                <div className="text-[10px] text-zinc-500 text-right">Saldo acumulado</div>
              </div>
            </div>

            {/* Categorías */}
            <div className="flex space-x-2 overflow-x-auto pb-1 no-scrollbar text-xs font-bold">
              {['TODOS', 'INDIVIDUAL', 'COMBO', 'BEBIDA', 'EXTRA'].map(cat => (
                <button
                  key={cat}
                  onClick={() => setFiltroCategoria(cat)}
                  className={`px-3 py-1.5 rounded-xl border transition ${
                    filtroCategoria === cat
                      ? 'bg-[#2563eb] text-white border-[#2563eb]'
                      : 'bg-[#111114] text-zinc-400 border-[#222227] hover:text-white'
                  }`}
                >
                  {cat}
                </button>
              ))}
            </div>

            {/* Modificadores rápidos: se pueden combinar varios */}
            <div className="bg-[#111114] border border-[#222227] p-2.5 rounded-2xl space-y-2">
              <div className="flex items-center justify-between">
                <span className="text-zinc-400 text-[11px] font-semibold">Modificadores (puedes elegir varios):</span>
                {modsSeleccionados.length > 0 && (
                  <button onClick={() => setModsSeleccionados([])} className="text-[10px] font-bold text-zinc-400 hover:text-red-400">
                    Limpiar
                  </button>
                )}
              </div>
              <div className="flex flex-wrap gap-1.5 text-xs">
                {MODIFICADORES_RAPIDOS.map(mod => (
                  <button
                    key={mod}
                    onClick={() => alternarMod(mod)}
                    className={`px-2.5 py-1 rounded-lg border text-[11px] font-bold transition ${
                      modsSeleccionados.includes(mod)
                        ? 'bg-amber-500 text-black border-amber-400'
                        : 'bg-[#09090b] text-zinc-300 border-[#222227] hover:border-zinc-500'
                    }`}
                  >
                    {mod}
                  </button>
                ))}
                {modsSeleccionados.filter(m => !MODIFICADORES_RAPIDOS.includes(m)).map(m => (
                  <button
                    key={m}
                    onClick={() => alternarMod(m)}
                    className="px-2.5 py-1 rounded-lg border text-[11px] font-bold bg-amber-500 text-black border-amber-400"
                  >
                    {m} ✕
                  </button>
                ))}
              </div>
              <div className="flex gap-2">
                <input
                  type="text"
                  value={modPersonalizado}
                  onChange={e => setModPersonalizado(e.target.value)}
                  onKeyDown={e => { if (e.key === 'Enter') agregarModPersonalizado(); }}
                  placeholder="Otro (ej. sin queso)"
                  className="flex-1 bg-[#09090b] border border-[#222227] rounded-lg px-2.5 py-1.5 text-[11px] text-white outline-none focus:border-amber-500"
                />
                <button
                  onClick={agregarModPersonalizado}
                  className="px-3 py-1.5 rounded-lg bg-zinc-800 text-zinc-200 text-[11px] font-bold hover:bg-zinc-700"
                >
                  Añadir
                </button>
              </div>
              {modsSeleccionados.length > 0 && (
                <div className="text-[10px] text-amber-400">
                  Se aplicará al próximo producto que agregues: {modsSeleccionados.join(' • ')}
                </div>
              )}
            </div>

            {/* Orden actual: corregir lo que se agregó sin querer */}
            {carrito.length > 0 && (
              <div className="bg-[#111114] border border-[#222227] rounded-2xl p-3 space-y-2">
                <div className="text-[11px] font-bold text-zinc-300 uppercase tracking-wider">Orden actual</div>
                <div className="space-y-1.5 max-h-44 overflow-y-auto">
                  {carrito.map((item, idx) => (
                    <div key={idx} className="flex items-center justify-between gap-2 bg-[#09090b] border border-[#222227] rounded-xl p-2">
                      <div className="min-w-0">
                        <div className="text-xs font-bold text-white truncate">{item.producto.nombre}</div>
                        {item.modificadores.length > 0 && (
                          <div className="text-[10px] text-amber-400">{item.modificadores.join(' • ')}</div>
                        )}
                      </div>
                      <div className="flex items-center space-x-1.5 shrink-0">
                        <button onClick={() => cambiarCantidad(idx, -1)} className="w-7 h-7 rounded-lg bg-zinc-800 text-white font-black">−</button>
                        <span className="w-5 text-center text-xs font-mono font-bold text-white">{item.cantidad}</span>
                        <button onClick={() => cambiarCantidad(idx, 1)} className="w-7 h-7 rounded-lg bg-zinc-800 text-white font-black">+</button>
                        <button onClick={() => quitarDelCarrito(idx)} className="w-7 h-7 rounded-lg text-zinc-400 hover:text-red-400" title="Quitar">✕</button>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Grid de Productos Táctiles */}
            <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-3">
              {productos
                .filter(p => p.activo && (filtroCategoria === 'TODOS' || p.tipo === filtroCategoria))
                .map(prod => {
                  const bsPrice = prod.precio_venta_usd * tasaActiva;
                  const enOrden = carrito.filter(i => i.producto.id === prod.id).reduce((a, i) => a + i.cantidad, 0);
                  return (
                    <div
                      key={prod.id}
                      className="bg-[#111114] border border-[#222227] hover:border-[#2563eb] transition p-3.5 rounded-2xl flex flex-col justify-between group"
                    >
                      <div>
                        <div className="flex items-center justify-between">
                          <span className="text-[9px] uppercase font-bold text-zinc-400 tracking-wider">
                            {prod.tipo}
                          </span>
                          <span className="text-[10px] text-zinc-500 group-hover:text-[#60a5fa] font-mono">
                            BOM: {prod.receta.length} insumos
                          </span>
                        </div>
                        <h3 className="font-extrabold text-sm text-white mt-1 leading-snug line-clamp-2">
                          {prod.nombre}
                        </h3>
                      </div>

                      <div className="mt-4 pt-2 border-t border-[#222227]/60 flex items-baseline justify-between">
                        <span className="text-base font-black text-white font-mono">
                          ${prod.precio_venta_usd.toFixed(2)}
                        </span>
                        <span className="text-xs font-mono font-bold text-[#60a5fa]">
                          Bs {bsPrice.toFixed(2)}
                        </span>
                      </div>
                      <button
                        onClick={() => agregarAlCarrito(prod, modsSeleccionados)}
                        className="mt-3 w-full py-2.5 rounded-xl bg-[#2563eb] hover:bg-[#1d4ed8] active:bg-[#1e40af] text-white text-xs font-bold transition"
                      >
                        + Agregar{enOrden > 0 ? ` (${enOrden} en la orden)` : ''}
                      </button>
                    </div>
                  );
                })}
            </div>
          </div>
        )}

        {/* VISTA 2: COCINA Y FIADOS */}
        {vistaActual === 'cocina' && (
          <div className="space-y-4">
            <div className="flex border-b border-[#222227] text-sm font-bold">
              <button
                onClick={() => setTabCocina('preparando')}
                className={`pb-2.5 px-4 flex items-center space-x-2 transition ${
                  tabCocina === 'preparando'
                    ? 'border-b-2 border-[#2563eb] text-[#2563eb]'
                    : 'text-zinc-400 hover:text-white'
                }`}
              >
                <ChefHat className="w-4 h-4" />
                <span>En Preparación ({ordenes.filter(o => o.estado_cocina === 'PREPARANDO' && o.sede_id === sedeActivaId).length})</span>
              </button>
              <button
                onClick={() => setTabCocina('fiados')}
                className={`pb-2.5 px-4 flex items-center space-x-2 transition ${
                  tabCocina === 'fiados'
                    ? 'border-b-2 border-[#2563eb] text-[#2563eb]'
                    : 'text-zinc-400 hover:text-white'
                }`}
              >
                <CreditCard className="w-4 h-4" />
                <span>Cuentas Fiadas ({ordenes.filter(o => o.estado_pago === 'FIADO').length})</span>
              </button>
            </div>

            {tabCocina === 'preparando' && (
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                {ordenes
                  .filter(o => o.estado_cocina === 'PREPARANDO' && o.estado_pago !== 'ANULADA' && o.sede_id === sedeActivaId)
                  .map(ord => (
                    <div
                      key={ord.id}
                      className="bg-[#111114] border border-[#222227] rounded-2xl p-4 flex flex-col justify-between space-y-3"
                    >
                      <div className="flex justify-between items-start">
                        <div>
                          <div className="flex items-center space-x-2">
                            <span className="font-mono text-xs font-black text-[#2563eb]">
                              #{ord.id.toString().slice(-4)}
                            </span>
                            <span className="text-xs font-bold text-white">{ord.cliente_nombre}</span>
                          </div>
                          <div className="text-[10px] text-zinc-400 mt-0.5">Hora: {ord.created_at}</div>
                        </div>
                        <span className="bg-amber-950/60 border border-amber-800 text-amber-400 text-[10px] font-bold px-2 py-0.5 rounded-full">
                          En Cocina
                        </span>
                      </div>

                      <div className="bg-[#09090b] p-3 rounded-xl border border-[#222227] space-y-1.5 text-xs">
                        {ord.items.map((it, idx) => (
                          <div key={idx} className="flex justify-between items-center">
                            <span className="font-bold text-white">
                              {it.cantidad}x {it.nombre}
                            </span>
                          </div>
                        ))}
                      </div>

                      <button
                        onClick={() => {
                          setOrdenes(prev =>
                            prev.map(o => (o.id === ord.id ? { ...o, estado_cocina: 'ENTREGADO' } : o))
                          );
                          fetch('/api/orden/estado', {
                            method: 'PATCH',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ orden_id: ord.id, estado_cocina: 'ENTREGADO' })
                          }).catch(err => console.warn(err));
                        }}
                        className="w-full py-2.5 bg-[#2563eb] hover:bg-[#1d4ed8] text-white text-xs font-bold rounded-xl flex items-center justify-center space-x-2 transition"
                      >
                        <CheckCircle2 className="w-4 h-4" />
                        <span>Marcar Entregado</span>
                      </button>
                    </div>
                  ))}

                {ordenes.filter(o => o.estado_cocina === 'PREPARANDO' && o.sede_id === sedeActivaId).length === 0 && (
                  <div className="col-span-full py-12 text-center text-zinc-500 text-xs font-medium">
                    No hay pedidos pendientes en la cola de cocina para {sedeActual.nombre}.
                  </div>
                )}
              </div>
            )}
          </div>
        )}

        {/* VISTA 3: STOCK DE SEDE Y MERMAS */}
        {vistaActual === 'stock' && (
          <div className="space-y-4">
            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
              <div>
                <h2 className="text-sm font-bold text-white uppercase tracking-wider">
                  Control de Stock por Sede ({sedeStockActual ? sedeStockActual.nombre : ''})
                </h2>
                <p className="text-xs text-zinc-400 mt-0.5">
                  Descuento automático por receta (BOM). Semáforo de reposición inmediata.
                </p>
              </div>

              <div className="flex flex-wrap items-center gap-2">
                {usuarioActual.rol === 'superusuario' && (
                  <select
                    value={sedeStockId}
                    onChange={e => setSedeStockSel(Number(e.target.value))}
                    className="bg-[#09090b] border border-[#222227] text-xs font-bold text-white rounded-xl px-3 py-1.5 focus:border-[#2563eb] outline-none"
                  >
                    {sedes.map(s => (
                      <option key={s.id} value={s.id}>{s.nombre}</option>
                    ))}
                  </select>
                )}
                {usuarioActual.rol === 'superusuario' && (
                  <>
                    <button
                      onClick={() => abrirMovimiento('COMPRA')}
                      className="bg-emerald-950/60 hover:bg-emerald-900/60 border border-emerald-800 text-emerald-300 px-3 py-1.5 rounded-xl text-xs font-bold transition"
                    >
                      + Entrada / Compra
                    </button>
                    <button
                      onClick={() => abrirMovimiento('TRANSFERENCIA')}
                      className="bg-blue-950/60 hover:bg-blue-900/60 border border-blue-800 text-blue-300 px-3 py-1.5 rounded-xl text-xs font-bold transition"
                    >
                      Transferir
                    </button>
                    <button
                      onClick={() => abrirInsumo(null)}
                      className="bg-zinc-800 hover:bg-zinc-700 border border-zinc-700 text-zinc-200 px-3 py-1.5 rounded-xl text-xs font-bold transition"
                    >
                      + Nuevo insumo
                    </button>
                  </>
                )}
                <button
                  onClick={() => setModalMermaAbierto(true)}
                  className="bg-red-950/60 hover:bg-red-900/60 border border-red-800 text-red-400 px-3 py-1.5 rounded-xl text-xs font-bold flex items-center space-x-1.5 transition"
                >
                  <AlertTriangle className="w-3.5 h-3.5" />
                  <span>Reportar Merma / Daño</span>
                </button>
              </div>
            </div>

            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
              {insumos.map(insumo => {
                const key = `${sedeStockId}_${insumo.id}`;
                const stockActual = inventarioSedes[key] ?? 0;
                const alerta = insumo.alerta_stock_minimo;
                const esCritico = stockActual <= alerta;
                const esAlerta = stockActual <= alerta * 1.5 && !esCritico;

                return (
                  <div
                    key={insumo.id}
                    className={`p-3.5 rounded-2xl border transition ${
                      esCritico
                        ? 'bg-red-950/20 border-red-800/80'
                        : esAlerta
                        ? 'bg-amber-950/20 border-amber-800/80'
                        : 'bg-[#111114] border-[#222227]'
                    }`}
                  >
                    <div className="flex justify-between items-start">
                      <div>
                        <span className="text-[10px] text-zinc-400 uppercase font-semibold">
                          Insumo #{insumo.id}
                        </span>
                        <h4 className="font-bold text-sm text-white">{insumo.nombre}</h4>
                        <div className="text-[10px] text-zinc-400 mt-1">
                          Costo Unit: ${insumo.costo_unitario_usd.toFixed(4)} / {insumo.unidad}
                        </div>
                        <div className="text-[10px] text-zinc-500">
                          Alerta mínima: {alerta} {insumo.unidad}
                        </div>
                      </div>

                      <div className="text-right">
                        <div
                          className={`text-xl font-mono font-black ${
                            esCritico ? 'text-red-400' : esAlerta ? 'text-amber-400' : 'text-emerald-400'
                          }`}
                        >
                          {stockActual}
                        </div>
                        <div className="text-[10px] uppercase font-bold text-zinc-500">
                          {insumo.unidad}
                        </div>
                      </div>
                    </div>

                    {usuarioActual.rol === 'superusuario' && (
                      <div className="mt-3 pt-2 border-t border-[#222227]/60 flex gap-2 text-[11px] font-bold">
                        <button
                          onClick={() => abrirMovimiento('COMPRA', insumo.id)}
                          className="flex-1 py-1.5 rounded-lg bg-emerald-900/30 border border-emerald-800 text-emerald-300 hover:bg-emerald-900/50"
                        >
                          + Entrada
                        </button>
                        <button
                          onClick={() => abrirMovimiento('AJUSTE', insumo.id)}
                          className="flex-1 py-1.5 rounded-lg bg-zinc-800 border border-zinc-700 text-zinc-200 hover:bg-zinc-700"
                        >
                          Ajustar
                        </button>
                        <button
                          onClick={() => abrirInsumo(insumo)}
                          className="flex-1 py-1.5 rounded-lg bg-zinc-800 border border-zinc-700 text-zinc-200 hover:bg-zinc-700"
                        >
                          Editar
                        </button>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        )}

        {/* VISTA 4: AUDITORÍA Y LOS 3 BOLSILLOS (Superusuario) */}
        {vistaActual === 'auditoria' && (
          <div className="space-y-4">
            {usuarioActual.rol !== 'superusuario' ? (
              <div className="bg-red-950/30 border border-red-800 p-6 rounded-2xl text-center space-y-2">
                <Lock className="w-8 h-8 text-red-400 mx-auto" />
                <h3 className="font-bold text-sm text-red-300">Acceso Restringido</h3>
                <p className="text-xs text-zinc-400">
                  El módulo de auditoría financiera requiere rol de Superusuario.
                </p>
              </div>
            ) : (
              <div className="space-y-4">
                <div className="flex justify-between items-center">
                  <div>
                    <h2 className="text-sm font-bold text-white uppercase tracking-wider">
                      Auditoría Financiera: Los 3 Bolsillos
                    </h2>
                    <p className="text-xs text-zinc-400">
                      Separación matemática del capital de reposición intocable, reserva operativa y utilidad neta real.
                    </p>
                  </div>
                </div>

                {/* 3 Bolsillos */}
                <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                  <div className="bg-[#111114] border border-[#222227] p-4 rounded-2xl">
                    <div className="text-[10px] text-zinc-400 uppercase font-black tracking-wider flex items-center space-x-1.5">
                      <span className="w-2 h-2 rounded-full bg-blue-500"></span>
                      <span>Bolsillo 1 (Intocable)</span>
                    </div>
                    <div className="text-2xl font-black font-mono text-blue-400 mt-2">
                      ${reporte1_Ventas.totalCostosBOM.toFixed(2)}
                    </div>
                    <div className="text-[11px] text-zinc-400 mt-1 leading-snug">
                      Costo exacto congelado de materias primas para reponer mercancía vendida.
                    </div>
                  </div>

                  <div className="bg-[#111114] border border-[#222227] p-4 rounded-2xl">
                    <div className="text-[10px] text-zinc-400 uppercase font-black tracking-wider flex items-center space-x-1.5">
                      <span className="w-2 h-2 rounded-full bg-amber-500"></span>
                      <span>Bolsillo 2 (Reserva Operativa)</span>
                    </div>
                    <div className="text-2xl font-black font-mono text-amber-400 mt-2">
                      ${((reporte1_Ventas.totalUSD - reporte1_Ventas.totalCostosBOM) * (pctReservaOperativa / 100) + gastosOperativosUSD).toFixed(2)}
                    </div>
                    <div className="text-[11px] text-zinc-400 mt-1 leading-snug">
                      {pctReservaOperativa}% Reserva Operativa + Gastos de jornada declarados.
                    </div>
                  </div>

                  <div className="bg-[#111114] border border-[#222227] p-4 rounded-2xl">
                    <div className="text-[10px] text-zinc-400 uppercase font-black tracking-wider flex items-center space-x-1.5">
                      <span className="w-2 h-2 rounded-full bg-emerald-500"></span>
                      <span>Bolsillo 3 (Utilidad Neta Real)</span>
                    </div>
                    <div className="text-2xl font-black font-mono text-emerald-400 mt-2">
                      ${Math.max(0, (reporte1_Ventas.totalUSD - reporte1_Ventas.totalCostosBOM) * (1 - pctReservaOperativa / 100) - gastosOperativosUSD).toFixed(2)}
                    </div>
                    <div className="text-[11px] text-zinc-400 mt-1 leading-snug">
                      Ganancia libre del dueño tras cubrir insumos, reposición y reservas.
                    </div>
                  </div>
                </div>

                {/* Historial con Auditoría */}
                <div className="bg-[#111114] border border-[#222227] rounded-2xl p-4 space-y-3">
                  <h3 className="text-xs font-bold text-white uppercase tracking-wider">
                    Historial de Ventas Recientes & Anulación con Reversión
                  </h3>
                  <div className="space-y-2">
                    {ordenes.slice(0, 10).map(ord => (
                      <div
                        key={ord.id}
                        className={`p-3 rounded-xl border flex flex-col sm:flex-row sm:items-center justify-between gap-3 text-xs ${
                          ord.estado_pago === 'ANULADA'
                            ? 'bg-zinc-900/60 border-zinc-800 opacity-60'
                            : 'bg-[#09090b] border-[#222227]'
                        }`}
                      >
                        <div>
                          <div className="flex items-center space-x-2">
                            <span className="font-mono font-black text-[#2563eb]">#{ord.id}</span>
                            <span className="font-bold text-white">{ord.cliente_nombre}</span>
                            <span className="text-[10px] text-zinc-400">
                              (Sede {ord.sede_id} • Cajero: {ord.cajero_nombre})
                            </span>
                            <span
                              className={`text-[9px] font-bold px-1.5 py-0.5 rounded ${
                                ord.estado_pago === 'PAGADA'
                                  ? 'bg-emerald-950 text-emerald-400 border border-emerald-800'
                                  : 'bg-red-950 text-red-400 border border-red-800'
                              }`}
                            >
                              {ord.estado_pago}
                            </span>
                          </div>
                          <div className="text-[11px] text-zinc-400 mt-1">
                            {ord.items.map(i => `${i.cantidad}x ${i.nombre}`).join(', ')} • Costo insumos: $
                            {ord.costo_insumos_usd.toFixed(2)}
                          </div>
                        </div>

                        <div className="flex items-center space-x-3">
                          <div className="text-right">
                            <div className="font-mono font-black text-white">${ord.total_venta_usd.toFixed(2)}</div>
                          </div>
                          {ord.estado_pago !== 'ANULADA' && (
                            <button
                              onClick={() => {
                                setOrdenParaAuditar(ord);
                                setModalAuditoriaAbierto(true);
                              }}
                              className="bg-red-950 hover:bg-red-900 border border-red-800 text-red-400 px-3 py-1.5 rounded-xl font-bold text-xs transition"
                            >
                              Auditar / Anular
                            </button>
                          )}
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            )}
          </div>
        )}

        {/* ============================================================== */}
        {/* VISTA 5: MÓDULO DE REPORTES AVANZADOS (SUPERUSUARIO)            */}
        {/* ============================================================== */}
        {vistaActual === 'reportes' && (
          <div className="space-y-5">
            {usuarioActual.rol !== 'superusuario' ? (
              <div className="bg-red-950/30 border border-red-800 p-6 rounded-2xl text-center space-y-2">
                <Lock className="w-8 h-8 text-red-400 mx-auto" />
                <h3 className="font-bold text-sm text-red-300">Módulo Reservado para el Superusuario</h3>
                <p className="text-xs text-zinc-400">
                  Este módulo es solo para el Superusuario. Cierra sesión e ingresa con esa cuenta para ver los reportes financieros.
                </p>
                <button
                  onClick={onLogout}
                  className="bg-[#2563eb] text-white text-xs font-bold px-4 py-2 rounded-xl mt-2"
                >
                  Cerrar sesión
                </button>
              </div>
            ) : (
              <div className="space-y-4">
                {/* Cabecera del Módulo de Reportes con Exportación */}
                <div className="flex flex-col md:flex-row md:items-center justify-between gap-3 pb-2 border-b border-[#222227]">
                  <div>
                    <div className="flex items-center space-x-2">
                      <BarChart3 className="w-5 h-5 text-[#2563eb]" />
                      <h2 className="text-base font-black text-white tracking-tight">
                        Centro de Reportes Ejecutivos & Inteligencia de Negocio
                      </h2>
                    </div>
                    <p className="text-xs text-zinc-400 mt-0.5">
                      Auditoría analítica bimonetaria, control de mermas, explosión de recetas y productividad de personal.
                    </p>
                  </div>

                  {/* Botones de Exportación CSV y PDF */}
                  <div className="flex items-center space-x-2">
                    <button
                      onClick={exportarCSV}
                      className="bg-[#111114] hover:bg-zinc-800 border border-[#222227] hover:border-emerald-500 text-white px-3.5 py-2 rounded-xl text-xs font-bold flex items-center space-x-2 transition "
                      title="Descargar datos en formato CSV con UTF-8 BOM"
                    >
                      <FileSpreadsheet className="w-4 h-4 text-emerald-400" />
                      <span>Exportar CSV</span>
                    </button>

                    <button
                      onClick={() => setModalReportePrintAbierto(true)}
                      className="bg-[#2563eb] hover:bg-[#1d4ed8] text-white px-3.5 py-2 rounded-xl text-xs font-bold flex items-center space-x-2 transition "
                      title="Ver vista de impresión o exportar a PDF"
                    >
                      <Printer className="w-4 h-4" />
                      <span>Generar PDF / Imprimir</span>
                    </button>
                  </div>
                </div>

                {/* Barra de Filtros: Rangos de Fecha Personalizables y Selector de Sede */}
                <div className="bg-[#111114] border border-[#222227] p-3.5 rounded-2xl flex flex-col lg:flex-row lg:items-center justify-between gap-3 text-xs">
                  {/* Selector de Presets de Fecha */}
                  <div className="flex items-center space-x-1.5 overflow-x-auto no-scrollbar pb-1 lg:pb-0">
                    <span className="text-zinc-400 text-[11px] font-bold mr-1 flex items-center">
                      <Calendar className="w-3.5 h-3.5 mr-1 text-[#2563eb]" /> Período:
                    </span>
                    {['hoy', 'ayer', '7dias', '30dias', 'personalizado'].map(preset => (
                      <button
                        key={preset}
                        onClick={() => setFiltroPresetFecha(preset)}
                        className={`px-3 py-1.5 rounded-xl border text-[11px] font-bold transition whitespace-nowrap ${
                          filtroPresetFecha === preset
                            ? 'bg-[#2563eb] text-white border-[#2563eb]'
                            : 'bg-[#09090b] text-zinc-400 border-[#222227] hover:text-white'
                        }`}
                      >
                        {preset === 'hoy'
                          ? 'Hoy'
                          : preset === 'ayer'
                          ? 'Ayer'
                          : preset === '7dias'
                          ? 'Últimos 7 días'
                          : preset === '30dias'
                          ? 'Últimos 30 días'
                          : 'Personalizado'}
                      </button>
                    ))}
                  </div>

                  {/* Pickers de Fecha Manual y Sede */}
                  <div className="flex flex-wrap items-center gap-2 pt-2 lg:pt-0 border-t lg:border-t-0 border-[#222227]">
                    <div className="flex items-center space-x-1.5 bg-[#09090b] border border-[#222227] px-2.5 py-1.5 rounded-xl">
                      <span className="text-zinc-500 text-[10px]">Desde:</span>
                      <input
                        type="date"
                        value={fechaInicioReporte}
                        onChange={e => {
                          setFechaInicioReporte(e.target.value);
                          setFiltroPresetFecha('personalizado');
                        }}
                        className="bg-transparent text-white text-xs outline-none font-mono"
                      />
                    </div>

                    <div className="flex items-center space-x-1.5 bg-[#09090b] border border-[#222227] px-2.5 py-1.5 rounded-xl">
                      <span className="text-zinc-500 text-[10px]">Hasta:</span>
                      <input
                        type="date"
                        value={fechaFinReporte}
                        onChange={e => {
                          setFechaFinReporte(e.target.value);
                          setFiltroPresetFecha('personalizado');
                        }}
                        className="bg-transparent text-white text-xs outline-none font-mono"
                      />
                    </div>

                    {/* Selector de Sede */}
                    <div className="flex items-center space-x-1.5 bg-[#09090b] border border-[#222227] px-2.5 py-1.5 rounded-xl">
                      <Store className="w-3.5 h-3.5 text-zinc-400" />
                      <select
                        value={filtroSedeReporte}
                        onChange={e => setFiltroSedeReporte(e.target.value === 'todas' ? 'todas' : Number(e.target.value))}
                        className="bg-transparent text-white text-xs outline-none font-bold cursor-pointer"
                      >
                        <option value="todas">Todas las Sedes (Consolidado)</option>
                        {sedes
                          .filter(s => !s.es_almacen)
                          .map(s => (
                            <option key={s.id} value={s.id}>
                              {s.nombre}
                            </option>
                          ))}
                      </select>
                    </div>
                  </div>
                </div>

                {/* Sub-Pestañas de Reportes */}
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-xs font-bold">
                  {[
                    { id: 'ventas', label: '1. Rendimiento de Ventas', icon: TrendingUp },
                    { id: 'inventario', label: '2. Rotación de Inventario', icon: Package },
                    { id: 'rentabilidad', label: '3. Rentabilidad (BOM)', icon: Percent },
                    { id: 'cajeros', label: '4. Productividad de Cajeros', icon: Users },
                  ].map(tab => {
                    const Icon = tab.icon;
                    const activo = reporteSubTab === tab.id;
                    return (
                      <button
                        key={tab.id}
                        onClick={() => setReporteSubTab(tab.id)}
                        className={`p-3 rounded-2xl border text-left flex items-center space-x-2.5 transition ${
                          activo
                            ? 'bg-[#2563eb]/20 border-[#2563eb] text-white '
                            : 'bg-[#111114] border-[#222227] text-zinc-400 hover:text-white hover:border-zinc-700'
                        }`}
                      >
                        <div
                          className={`w-7 h-7 rounded-lg flex items-center justify-center ${
                            activo ? 'bg-[#2563eb] text-white' : 'bg-zinc-800 text-zinc-400'
                          }`}
                        >
                          <Icon className="w-4 h-4" />
                        </div>
                        <span className="leading-snug">{tab.label}</span>
                      </button>
                    );
                  })}
                </div>

                {/* ============================================================== */}
                {/* SUB-REPORTE 1: RENDIMIENTO DE VENTAS                           */}
                {/* ============================================================== */}
                {reporteSubTab === 'ventas' && (
                  <div className="space-y-4">
                    {/* Tarjetas KPI de Ventas */}
                    <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
                      <div className="bg-[#111114] border border-[#222227] p-4 rounded-2xl">
                        <span className="text-[10px] text-zinc-400 uppercase font-black tracking-wider block">
                          Ingresos Brutos Totales
                        </span>
                        <div className="text-2xl font-black font-mono text-white mt-1">
                          ${reporte1_Ventas.totalUSD.toFixed(2)}
                        </div>
                        <span className="text-xs font-mono font-bold text-[#60a5fa] block mt-0.5">
                          Bs {(reporte1_Ventas.totalUSD * tasaActiva).toFixed(2)}
                        </span>
                      </div>

                      <div className="bg-[#111114] border border-[#222227] p-4 rounded-2xl">
                        <span className="text-[10px] text-zinc-400 uppercase font-black tracking-wider block">
                          Ticket Promedio / Orden
                        </span>
                        <div className="text-2xl font-black font-mono text-emerald-400 mt-1">
                          ${reporte1_Ventas.ticketPromedio.toFixed(2)}
                        </div>
                        <span className="text-[11px] text-zinc-400 mt-0.5 block">
                          {reporte1_Ventas.cantOrdenes} órdenes facturadas
                        </span>
                      </div>

                      <div className="bg-[#111114] border border-[#222227] p-4 rounded-2xl">
                        <span className="text-[10px] text-zinc-400 uppercase font-black tracking-wider block">
                          Efectivo Físico USD
                        </span>
                        <div className="text-2xl font-black font-mono text-blue-400 mt-1">
                          ${(reporte1_Ventas.totalRecibidoUSD - reporte1_Ventas.totalVueltoUSD).toFixed(2)}
                        </div>
                        <span className="text-[11px] text-zinc-400 mt-0.5 block">
                          Recibido: ${reporte1_Ventas.totalRecibidoUSD.toFixed(2)} | Vueltos: ${reporte1_Ventas.totalVueltoUSD.toFixed(2)}
                        </span>
                      </div>

                      <div className="bg-[#111114] border border-[#222227] p-4 rounded-2xl">
                        <span className="text-[10px] text-zinc-400 uppercase font-black tracking-wider block">
                          Pago Móvil Bs
                        </span>
                        <div className="text-2xl font-black font-mono text-[#38bdf8] mt-1">
                          Bs {(reporte1_Ventas.totalRecibidoBS - reporte1_Ventas.totalVueltoBS).toFixed(2)}
                        </div>
                        <span className="text-[11px] text-zinc-400 mt-0.5 block">
                          Entró por banco de forma digital
                        </span>
                      </div>
                    </div>

                    {/* Gráfico de Barras de Ventas por Día */}
                    <div className="bg-[#111114] border border-[#222227] p-4 rounded-2xl space-y-3">
                      <div className="flex justify-between items-center">
                        <h3 className="text-xs font-bold text-white uppercase tracking-wider flex items-center space-x-1.5">
                          <BarChart3 className="w-4 h-4 text-[#2563eb]" />
                          <span>Evolución Diaria de Ventas (USD)</span>
                        </h3>
                        <span className="text-[10px] text-zinc-400">Total días evaluados: {reporte1_Ventas.listaDias.length}</span>
                      </div>

                      {reporte1_Ventas.listaDias.length > 0 ? (
                        <div className="space-y-2 pt-2">
                          {reporte1_Ventas.listaDias.map(dia => {
                            const maxUSD = Math.max(...reporte1_Ventas.listaDias.map(d => d.totalUSD), 1);
                            const porcentaje = (dia.totalUSD / maxUSD) * 100;
                            return (
                              <div key={dia.fecha} className="flex items-center text-xs space-x-3">
                                <span className="w-20 font-mono text-zinc-400 text-[11px]">{dia.fecha}</span>
                                <div className="flex-1 bg-[#09090b] h-6 rounded-lg overflow-hidden border border-[#222227] relative">
                                  <div
                                    className="bg-gradient-to-r from-blue-600 to-[#2563eb] h-full rounded-lg transition-all duration-500"
                                    style={{ width: `${Math.max(5, porcentaje)}%` }}
                                  ></div>
                                  <span className="absolute inset-y-0 right-2 flex items-center font-mono font-bold text-white text-[11px]">
                                    ${dia.totalUSD.toFixed(2)} ({dia.ordenes} ord)
                                  </span>
                                </div>
                              </div>
                            );
                          })}
                        </div>
                      ) : (
                        <div className="py-8 text-center text-zinc-500 text-xs">
                          No se registraron ventas en el rango de fechas seleccionado.
                        </div>
                      )}
                    </div>

                    {/* Tabla Detallada de Órdenes */}
                    <div className="bg-[#111114] border border-[#222227] rounded-2xl overflow-hidden">
                      <div className="p-3.5 border-b border-[#222227] flex justify-between items-center">
                        <h4 className="text-xs font-bold text-white uppercase tracking-wider">
                          Registro de Transacciones Filtradas ({reporte1_Ventas.ordenes.length})
                        </h4>
                      </div>
                      <div className="overflow-x-auto">
                        <table className="w-full text-left text-xs">
                          <thead className="bg-[#09090b] text-zinc-400 uppercase text-[10px] font-bold border-b border-[#222227]">
                            <tr>
                              <th className="p-3">ID</th>
                              <th className="p-3">Fecha</th>
                              <th className="p-3">Sede</th>
                              <th className="p-3">Cajero</th>
                              <th className="p-3">Cliente</th>
                              <th className="p-3 text-right">Total USD</th>
                              <th className="p-3 text-right">Recibido USD</th>
                              <th className="p-3 text-right">Recibido Bs</th>
                            </tr>
                          </thead>
                          <tbody className="divide-y divide-[#27272a]">
                            {reporte1_Ventas.ordenes.map(ord => (
                              <tr key={ord.id} className="hover:bg-zinc-800/40">
                                <td className="p-3 font-mono font-bold text-[#2563eb]">#{ord.id}</td>
                                <td className="p-3 font-mono text-zinc-300">{ord.created_at}</td>
                                <td className="p-3 font-medium">Sede {ord.sede_id}</td>
                                <td className="p-3 text-zinc-300">{ord.cajero_nombre}</td>
                                <td className="p-3 text-white font-medium">{ord.cliente_nombre}</td>
                                <td className="p-3 text-right font-mono font-black text-emerald-400">
                                  ${ord.total_venta_usd.toFixed(2)}
                                </td>
                                <td className="p-3 text-right font-mono text-zinc-300">
                                  ${ord.recibido_usd.toFixed(2)}
                                </td>
                                <td className="p-3 text-right font-mono text-[#60a5fa]">
                                  Bs {ord.recibido_bs.toFixed(2)}
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    </div>
                  </div>
                )}

                {/* ============================================================== */}
                {/* SUB-REPORTE 2: ROTACIÓN DE INVENTARIO (BOM)                     */}
                {/* ============================================================== */}
                {reporteSubTab === 'inventario' && (
                  <div className="space-y-4">
                    <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                      <div className="bg-[#111114] border border-[#222227] p-4 rounded-2xl">
                        <span className="text-[10px] text-zinc-400 uppercase font-black tracking-wider block">
                          Valor de Insumos Consumidos
                        </span>
                        <div className="text-2xl font-black font-mono text-blue-400 mt-1">
                          ${reporte2_Inventario.totalValorConsumido.toFixed(2)}
                        </div>
                        <span className="text-[11px] text-zinc-400 mt-0.5 block">
                          Materia prima convertida en ventas vía recetas
                        </span>
                      </div>

                      <div className="bg-[#111114] border border-[#222227] p-4 rounded-2xl">
                        <span className="text-[10px] text-zinc-400 uppercase font-black tracking-wider block">
                          Insumos en Alta Rotación
                        </span>
                        <div className="text-2xl font-black font-mono text-amber-400 mt-1">
                          {reporte2_Inventario.tablaInsumos.filter(i => i.velocidad === 'ALTA').length}
                        </div>
                        <span className="text-[11px] text-zinc-400 mt-0.5 block">
                          Panes, salchichas y refrescos de rápido agotamiento
                        </span>
                      </div>

                      <div className="bg-[#111114] border border-[#222227] p-4 rounded-2xl">
                        <span className="text-[10px] text-zinc-400 uppercase font-black tracking-wider block">
                          Insumos Bajo Punto de Reorden
                        </span>
                        <div className="text-2xl font-black font-mono text-red-400 mt-1">
                          {reporte2_Inventario.tablaInsumos.filter(i => i.stockActual <= i.alerta_stock_minimo).length}
                        </div>
                        <span className="text-[11px] text-zinc-400 mt-0.5 block">
                          Requieren transferencia urgente de Almacén
                        </span>
                      </div>
                    </div>

                    {/* Tabla de Rotación por Insumo */}
                    <div className="bg-[#111114] border border-[#222227] rounded-2xl overflow-hidden">
                      <div className="p-3.5 border-b border-[#222227] flex justify-between items-center">
                        <h4 className="text-xs font-bold text-white uppercase tracking-wider">
                          Análisis de Insumos del BOM Maestro ({reporte2_Inventario.tablaInsumos.length})
                        </h4>
                      </div>
                      <div className="overflow-x-auto">
                        <table className="w-full text-left text-xs">
                          <thead className="bg-[#09090b] text-zinc-400 uppercase text-[10px] font-bold border-b border-[#222227]">
                            <tr>
                              <th className="p-3">Insumo</th>
                              <th className="p-3">Unidad</th>
                              <th className="p-3 text-right">Consumo BOM</th>
                              <th className="p-3 text-right">Stock Actual</th>
                              <th className="p-3 text-right">Costo Unitario</th>
                              <th className="p-3 text-right">Valor Consumido USD</th>
                              <th className="p-3 text-center">Velocidad</th>
                              <th className="p-3 text-right">Días Restantes</th>
                            </tr>
                          </thead>
                          <tbody className="divide-y divide-[#27272a]">
                            {reporte2_Inventario.tablaInsumos.map(ins => (
                              <tr key={ins.id} className="hover:bg-zinc-800/40">
                                <td className="p-3 font-bold text-white">{ins.nombre}</td>
                                <td className="p-3 text-zinc-400 uppercase text-[10px]">{ins.unidad}</td>
                                <td className="p-3 text-right font-mono font-bold text-blue-400">{ins.consumo}</td>
                                <td className="p-3 text-right font-mono font-bold text-zinc-200">{ins.stockActual}</td>
                                <td className="p-3 text-right font-mono text-zinc-400">
                                  ${ins.costo_unitario_usd.toFixed(4)}
                                </td>
                                <td className="p-3 text-right font-mono font-bold text-emerald-400">
                                  ${ins.valorConsumidoUSD.toFixed(2)}
                                </td>
                                <td className="p-3 text-center">
                                  <span
                                    className={`px-2 py-0.5 rounded text-[10px] font-bold ${
                                      ins.velocidad === 'ALTA'
                                        ? 'bg-red-950 text-red-400 border border-red-800'
                                        : ins.velocidad === 'MEDIA'
                                        ? 'bg-amber-950 text-amber-400 border border-amber-800'
                                        : 'bg-zinc-800 text-zinc-300'
                                    }`}
                                  >
                                    {ins.velocidad}
                                  </span>
                                </td>
                                <td className="p-3 text-right font-mono text-zinc-300">
                                  {ins.diasRestantes > 365 ? '∞' : `${ins.diasRestantes} d`}
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    </div>
                  </div>
                )}

                {/* ============================================================== */}
                {/* SUB-REPORTE 3: RENTABILIDAD POR ÍTEM Y UBICACIÓN               */}
                {/* ============================================================== */}
                {reporteSubTab === 'rentabilidad' && (
                  <div className="space-y-4">
                    {/* Tarjetas KPI de Rentabilidad */}
                    <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                      <div className="bg-[#111114] border border-[#222227] p-4 rounded-2xl">
                        <span className="text-[10px] text-zinc-400 uppercase font-black tracking-wider block">
                          Margen de Ganancia Promedio
                        </span>
                        <div className="text-2xl font-black font-mono text-emerald-400 mt-1">
                          {reporte3_Rentabilidad.margenPromedioGlobal.toFixed(1)}%
                        </div>
                        <span className="text-[11px] text-zinc-400 mt-0.5 block">
                          Utilidad Bruta: ${(reporte3_Rentabilidad.totalIngresos - reporte3_Rentabilidad.totalCostos).toFixed(2)} USD
                        </span>
                      </div>

                      <div className="bg-[#111114] border border-[#222227] p-4 rounded-2xl">
                        <span className="text-[10px] text-zinc-400 uppercase font-black tracking-wider block">
                          Total Facturación Bruta
                        </span>
                        <div className="text-2xl font-black font-mono text-white mt-1">
                          ${reporte3_Rentabilidad.totalIngresos.toFixed(2)}
                        </div>
                        <span className="text-[11px] text-zinc-400 mt-0.5 block">
                          Ventas de todos los productos del catálogo
                        </span>
                      </div>

                      <div className="bg-[#111114] border border-[#222227] p-4 rounded-2xl">
                        <span className="text-[10px] text-zinc-400 uppercase font-black tracking-wider block">
                          Costo BOM Congelado
                        </span>
                        <div className="text-2xl font-black font-mono text-blue-400 mt-1">
                          ${reporte3_Rentabilidad.totalCostos.toFixed(2)}
                        </div>
                        <span className="text-[11px] text-zinc-400 mt-0.5 block">
                          Destinado íntegro al Bolsillo 1 de Reposición
                        </span>
                      </div>
                    </div>

                    {/* Comparativa por Sede */}
                    <div className="bg-[#111114] border border-[#222227] p-4 rounded-2xl space-y-3">
                      <h4 className="text-xs font-bold text-white uppercase tracking-wider">
                        Comparativa de Desempeño por Ubicación
                      </h4>
                      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                        {reporte3_Rentabilidad.sedesAnalizadas.map(sedeStat => (
                          <div
                            key={sedeStat.sede_id}
                            className="bg-[#09090b] border border-[#222227] p-3.5 rounded-xl space-y-2"
                          >
                            <div className="flex justify-between items-center">
                              <span className="font-extrabold text-white text-xs">{sedeStat.nombre}</span>
                              <span className="text-[10px] bg-blue-950 text-blue-400 border border-blue-800 px-2 py-0.5 rounded font-bold">
                                {sedeStat.ordenes} órdenes
                              </span>
                            </div>
                            <div className="grid grid-cols-3 gap-2 pt-1 text-center">
                              <div className="bg-[#111114] p-2 rounded-lg">
                                <span className="text-[9px] text-zinc-400 uppercase font-bold block">Ventas</span>
                                <span className="font-mono font-bold text-white text-xs">
                                  ${sedeStat.totalUSD.toFixed(2)}
                                </span>
                              </div>
                              <div className="bg-[#111114] p-2 rounded-lg">
                                <span className="text-[9px] text-zinc-400 uppercase font-bold block">Costo BOM</span>
                                <span className="font-mono font-bold text-blue-400 text-xs">
                                  ${sedeStat.costoBOM.toFixed(2)}
                                </span>
                              </div>
                              <div className="bg-[#111114] p-2 rounded-lg">
                                <span className="text-[9px] text-zinc-400 uppercase font-bold block">Margen</span>
                                <span className="font-mono font-bold text-emerald-400 text-xs">
                                  {sedeStat.margenPct.toFixed(1)}%
                                </span>
                              </div>
                            </div>
                          </div>
                        ))}
                      </div>
                    </div>

                    {/* Tabla de Rentabilidad por Producto */}
                    <div className="bg-[#111114] border border-[#222227] rounded-2xl overflow-hidden">
                      <div className="p-3.5 border-b border-[#222227]">
                        <h4 className="text-xs font-bold text-white uppercase tracking-wider">
                          Rentabilidad y Margen por Producto
                        </h4>
                      </div>
                      <div className="overflow-x-auto">
                        <table className="w-full text-left text-xs">
                          <thead className="bg-[#09090b] text-zinc-400 uppercase text-[10px] font-bold border-b border-[#222227]">
                            <tr>
                              <th className="p-3">Producto</th>
                              <th className="p-3">Tipo</th>
                              <th className="p-3 text-right">Vendidos</th>
                              <th className="p-3 text-right">Ingresos USD</th>
                              <th className="p-3 text-right">Costo Insumos</th>
                              <th className="p-3 text-right">Ganancia Bruta</th>
                              <th className="p-3 text-right">Margen %</th>
                            </tr>
                          </thead>
                          <tbody className="divide-y divide-[#27272a]">
                            {reporte3_Rentabilidad.tablaProductos.map(prodStat => (
                              <tr key={prodStat.producto.id} className="hover:bg-zinc-800/40">
                                <td className="p-3 font-bold text-white">{prodStat.producto.nombre}</td>
                                <td className="p-3 text-zinc-400 text-[10px] uppercase">{prodStat.producto.tipo}</td>
                                <td className="p-3 text-right font-mono font-bold text-white">{prodStat.cant}</td>
                                <td className="p-3 text-right font-mono font-bold text-white">
                                  ${prodStat.ingresosUSD.toFixed(2)}
                                </td>
                                <td className="p-3 text-right font-mono text-zinc-400">
                                  ${prodStat.costoBOM.toFixed(2)}
                                </td>
                                <td className="p-3 text-right font-mono font-black text-emerald-400">
                                  ${prodStat.gananciaBruta.toFixed(2)}
                                </td>
                                <td className="p-3 text-right font-mono font-bold text-[#60a5fa]">
                                  {prodStat.margenPct.toFixed(1)}%
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    </div>
                  </div>
                )}

                {/* ============================================================== */}
                {/* SUB-REPORTE 4: DESEMPEÑO DE CAJEROS                            */}
                {/* ============================================================== */}
                {reporteSubTab === 'cajeros' && (
                  <div className="space-y-4">
                    <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                      {reporte4_Cajeros.map(cajero => (
                        <div
                          key={cajero.id}
                          className="bg-[#111114] border border-[#222227] p-4 rounded-2xl space-y-3"
                        >
                          <div className="flex justify-between items-start">
                            <div>
                              <span className="text-[10px] text-zinc-400 uppercase font-bold block">
                                {cajero.sedeNombre}
                              </span>
                              <h4 className="font-extrabold text-sm text-white mt-0.5">{cajero.nombre}</h4>
                            </div>
                            <span className="text-[10px] bg-zinc-800 px-2 py-0.5 rounded font-bold uppercase text-zinc-300">
                              {cajero.rol}
                            </span>
                          </div>

                          <div className="grid grid-cols-2 gap-2 pt-1 text-center">
                            <div className="bg-[#09090b] p-2.5 rounded-xl border border-[#222227]">
                              <span className="text-[9px] text-zinc-400 uppercase font-bold block">Órdenes</span>
                              <span className="font-mono font-black text-white text-base">
                                {cajero.ordenesProcesadas}
                              </span>
                            </div>
                            <div className="bg-[#09090b] p-2.5 rounded-xl border border-[#222227]">
                              <span className="text-[9px] text-zinc-400 uppercase font-bold block">Facturado</span>
                              <span className="font-mono font-black text-emerald-400 text-base">
                                ${cajero.totalUSD.toFixed(2)}
                              </span>
                            </div>
                          </div>

                          <div className="pt-2 border-t border-[#222227] flex justify-between items-center text-xs">
                            <span className="text-zinc-400">Ticket Promedio:</span>
                            <span className="font-mono font-bold text-white">
                              ${cajero.ticketPromedio.toFixed(2)}
                            </span>
                          </div>

                          <div className="flex justify-between items-center text-xs">
                            <span className="text-zinc-400">Diferencia Arqueo:</span>
                            <span className="bg-emerald-950 text-emerald-400 border border-emerald-800 px-2 py-0.5 rounded text-[10px] font-bold">
                              ✓ $0.00 Cuadrado
                            </span>
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>
        )}

        {/* VISTA 6: CONFIGURACIÓN Y PERSONAL */}
        {vistaActual === 'config' && (
          <div className="space-y-4">
            <h2 className="text-sm font-bold text-white uppercase tracking-wider">
              Administración y Cero Hardcoding
            </h2>
            <div className="bg-[#111114] border border-[#222227] p-4 rounded-2xl space-y-3">
              <h3 className="text-xs font-bold text-white uppercase">Motores de Tasas de Cambio</h3>
              <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 text-xs">
                <div>
                  <label className="text-zinc-400 block mb-1">Tasa Oficial BCV</label>
                  <input
                    type="number"
                    value={tasaBCV}
                    onChange={e => setTasaBCV(parseFloat(e.target.value) || 0)}
                    className="w-full bg-[#09090b] border border-[#222227] rounded-xl p-2.5 text-white font-mono font-bold"
                  />
                </div>
                <div>
                  <label className="text-zinc-400 block mb-1">Tasa Manual POS</label>
                  <input
                    type="number"
                    value={tasaManual}
                    onChange={e => setTasaManual(parseFloat(e.target.value) || 0)}
                    className="w-full bg-[#09090b] border border-[#222227] rounded-xl p-2.5 text-white font-mono font-bold"
                  />
                </div>
                <div>
                  <label className="text-zinc-400 block mb-1">Tasa de Reposición (Poder Real)</label>
                  <input
                    type="number"
                    value={tasaReposicion}
                    onChange={e => setTasaReposicion(parseFloat(e.target.value) || 0)}
                    className="w-full bg-[#09090b] border border-[#222227] rounded-xl p-2.5 text-white font-mono font-bold"
                  />
                </div>
              </div>
              <div className="flex items-center justify-between pt-2">
                <label className="flex items-center space-x-2 text-xs text-zinc-300 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={usarTasaManual}
                    onChange={e => setUsarTasaManual(e.target.checked)}
                    className="rounded border-zinc-700"
                  />
                  <span>Cobrar con Tasa Manual en lugar de Tasa Oficial BCV</span>
                </label>
                <button
                  onClick={() => {
                    fetch('/api/tasa/sync', {
                      method: 'POST',
                      headers: { 'Content-Type': 'application/json' },
                      body: JSON.stringify({
                        tasa_manual: tasaManual,
                        tasa_reposicion: tasaReposicion,
                        usar_tasa_manual: usarTasaManual,
                        pct_reserva_operativa: pctReservaOperativa
                      })
                    }).then(() => alert('Tasas guardadas en la base de datos.'));
                  }}
                  className="bg-[#2563eb] hover:bg-[#1d4ed8] text-white text-xs font-bold px-4 py-2 rounded-xl"
                >
                  Guardar Tasas en BD
                </button>
              </div>
            </div>

            {/* Configuración de Datos de Pago Móvil y Carga de Imagen QR */}
            <div className="bg-[#111114] border border-[#222227] p-4 rounded-2xl space-y-3 text-xs">
              <h3 className="text-xs font-bold text-white uppercase flex items-center space-x-2">
                <QrCode className="w-4 h-4 text-[#2563eb]" />
                <span>Módulo de Pago Móvil y Código QR del Banco</span>
              </h3>
              <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                <div>
                  <label className="text-zinc-400 block mb-1">Banco Receptor</label>
                  <input
                    type="text"
                    value={metodosQR[0]?.banco || ''}
                    onChange={e => {
                      const copia = [...metodosQR];
                      copia[0] = { ...copia[0], banco: e.target.value };
                      setMetodosQR(copia);
                    }}
                    className="w-full bg-[#09090b] border border-[#222227] rounded-xl p-2.5 text-white font-bold"
                  />
                </div>
                <div>
                  <label className="text-zinc-400 block mb-1">Teléfono Pago Móvil</label>
                  <input
                    type="text"
                    value={metodosQR[0]?.telefono || ''}
                    onChange={e => {
                      const copia = [...metodosQR];
                      copia[0] = { ...copia[0], telefono: e.target.value };
                      setMetodosQR(copia);
                    }}
                    className="w-full bg-[#09090b] border border-[#222227] rounded-xl p-2.5 text-white font-mono font-bold"
                  />
                </div>
                <div>
                  <label className="text-zinc-400 block mb-1">Cédula / RIF</label>
                  <input
                    type="text"
                    value={metodosQR[0]?.rif_cedula || ''}
                    onChange={e => {
                      const copia = [...metodosQR];
                      copia[0] = { ...copia[0], rif_cedula: e.target.value };
                      setMetodosQR(copia);
                    }}
                    className="w-full bg-[#09090b] border border-[#222227] rounded-xl p-2.5 text-white font-mono font-bold"
                  />
                </div>
              </div>
              <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 pt-2">
                <div>
                  <label className="text-zinc-400 block mb-1">Subir Imagen QR del Banco (Se guarda en Base64 en la BD)</label>
                  <input
                    type="file"
                    accept="image/*"
                    onChange={e => {
                      const file = e.target.files?.[0];
                      if (!file) return;
                      const reader = new FileReader();
                      reader.onload = ev => {
                        const base64 = ev.target && ev.target.result ? String(ev.target.result) : '';
                        const copia = [...metodosQR];
                        copia[0] = { ...copia[0], qr_base64: base64 };
                        setMetodosQR(copia);
                      };
                      reader.readAsDataURL(file);
                    }}
                    className="text-xs text-zinc-300"
                  />
                </div>
                <button
                  onClick={() => {
                    fetch('/api/admin/crud', {
                      method: 'POST',
                      headers: { 'Content-Type': 'application/json' },
                      body: JSON.stringify({
                        entidad: 'metodos_qr',
                        accion: 'UPDATE',
                        registro: metodosQR[0]
                      })
                    }).then(() => alert('Datos de Pago Móvil y QR guardados correctamente.'));
                  }}
                  className="bg-emerald-600 hover:bg-emerald-500 text-white font-bold px-4 py-2.5 rounded-xl"
                >
                  Guardar Pago Móvil y QR
                </button>
              </div>
            </div>
          </div>
        )}

        {/* VISTA 7: DESPLIEGUE RENDER & ARCHIVOS */}
        {vistaActual === 'deploy' && (
          <div className="space-y-4">
            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 bg-[#111114] border border-blue-900/50 p-4 rounded-2xl">
              <div>
                <h2 className="text-sm font-bold text-white uppercase tracking-wider flex items-center space-x-2">
                  <Download className="w-4 h-4 text-[#2563eb]" />
                  <span>Paquete Completo de Despliegue (.ZIP)</span>
                </h2>
                <p className="text-xs text-zinc-400 mt-0.5">
                  Descarga todos los archivos creados (render.yaml, requirements.txt, server.py, supabase_schema.sql, README.md).
                </p>
              </div>
              <a
                href="/burgeradmin_deploy.zip"
                download="burgeradmin_deploy.zip"
                className="bg-[#2563eb] hover:bg-[#1d4ed8] text-white px-4 py-2.5 rounded-xl text-xs font-bold flex items-center justify-center space-x-2  transition whitespace-nowrap"
              >
                <Download className="w-4 h-4" />
                <span>Descargar burgeradmin_deploy.zip</span>
              </a>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
              <div className="bg-[#111114] border border-[#222227] p-4 rounded-2xl">
                <div className="flex items-center space-x-2 text-white font-bold text-xs mb-2">
                  <FileCode className="w-4 h-4 text-blue-400" />
                  <span>render.yaml</span>
                </div>
                <div className="text-[10px] bg-emerald-950/60 border border-emerald-800 text-emerald-400 px-2 py-1 rounded font-bold inline-block">
                  ✓ Configurado en la raíz
                </div>
              </div>

              <div className="bg-[#111114] border border-[#222227] p-4 rounded-2xl">
                <div className="flex items-center space-x-2 text-white font-bold text-xs mb-2">
                  <FileCode className="w-4 h-4 text-amber-400" />
                  <span>requirements.txt</span>
                </div>
                <div className="text-[10px] bg-emerald-950/60 border border-emerald-800 text-emerald-400 px-2 py-1 rounded font-bold inline-block">
                  ✓ Configurado en la raíz
                </div>
              </div>

              <div className="bg-[#111114] border border-[#222227] p-4 rounded-2xl">
                <div className="flex items-center space-x-2 text-white font-bold text-xs mb-2">
                  <FileCode className="w-4 h-4 text-emerald-400" />
                  <span>server.py</span>
                </div>
                <div className="text-[10px] bg-emerald-950/60 border border-emerald-800 text-emerald-400 px-2 py-1 rounded font-bold inline-block">
                  ✓ Configurado con API de reportes
                </div>
              </div>
            </div>
          </div>
        )}
      </main>

      {/* ============================================================== */}
      {/* BARRA FLOTANTE DE COBRO (VISTA POS)                            */}
      {/* ============================================================== */}
      {vistaActual === 'pos' && (
        <div className="fixed bottom-16 left-0 right-0 max-w-6xl mx-auto px-4 z-30 print:hidden">
          <div className="bg-[#111114]/95 backdrop-blur border border-[#222227] rounded-2xl p-3.5  flex items-center justify-between">
            <div className="flex items-center space-x-4">
              <div className="relative">
                <div className="w-10 h-10 rounded-xl bg-zinc-800 flex items-center justify-center text-white">
                  <ShoppingBag className="w-5 h-5 text-[#2563eb]" />
                </div>
                {carrito.length > 0 && (
                  <span className="absolute -top-1 -right-1 bg-[#2563eb] text-white text-[10px] font-bold w-5 h-5 rounded-full flex items-center justify-center border-2 border-[#18181b]">
                    {carrito.reduce((acc, i) => acc + i.cantidad, 0)}
                  </span>
                )}
              </div>

              <div>
                <div className="text-[10px] text-zinc-400 uppercase font-bold">Total a Cobrar</div>
                <div className="flex items-baseline space-x-2">
                  <span className="text-xl font-black text-white font-mono">
                    ${totalCarritoUSD.toFixed(2)}
                  </span>
                  <span className="text-xs font-mono font-bold text-[#60a5fa]">
                    Bs {totalCarritoBS.toFixed(2)}
                  </span>
                </div>
              </div>
            </div>

            <div className="flex items-center space-x-2">
              {carrito.length > 0 && (
                <button
                  onClick={() => setCarrito([])}
                  className="p-2.5 rounded-xl text-zinc-400 hover:text-red-400 hover:bg-zinc-800 transition"
                  title="Vaciar orden"
                >
                  <Trash2 className="w-4 h-4" />
                </button>
              )}

              <button
                disabled={carrito.length === 0}
                onClick={() => {
                  setCobroRecibidoUSD(totalCarritoUSD);
                  setCobroRecibidoBS(0);
                  setCobroVueltoUSD(0);
                  setCobroVueltoBS(0);
                  setModalCobroAbierto(true);
                }}
                className="bg-[#2563eb] hover:bg-[#1d4ed8] disabled:opacity-40 disabled:cursor-not-allowed text-white text-xs font-bold px-5 py-3 rounded-xl transition flex items-center space-x-2 "
              >
                <span>Cobrar Orden</span>
                <ArrowRight className="w-4 h-4" />
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ============================================================== */}
      {/* BARRA DE NAVEGACIÓN INFERIOR (MOBILE BOTTOM NAV)               */}
      {/* ============================================================== */}
      <nav className="fixed bottom-0 left-0 right-0 z-40 bg-[#09090b] border-t border-[#222227] max-w-6xl mx-auto flex items-center justify-around py-2 print:hidden">
        <button
          onClick={() => setVistaActual('pos')}
          className={`flex flex-col items-center transition ${
            vistaActual === 'pos' ? 'text-[#2563eb]' : 'text-zinc-500 hover:text-zinc-300'
          }`}
        >
          <ShoppingBag className="w-5 h-5" />
          <span className="text-[10px] font-medium mt-0.5">Caja</span>
        </button>

        <button
          onClick={() => setVistaActual('cocina')}
          className={`flex flex-col items-center transition ${
            vistaActual === 'cocina' ? 'text-[#2563eb]' : 'text-zinc-500 hover:text-zinc-300'
          }`}
        >
          <ChefHat className="w-5 h-5" />
          <span className="text-[10px] font-medium mt-0.5">Cocina</span>
        </button>

        <button
          onClick={() => setVistaActual('stock')}
          className={`flex flex-col items-center transition ${
            vistaActual === 'stock' ? 'text-[#2563eb]' : 'text-zinc-500 hover:text-zinc-300'
          }`}
        >
          <Package className="w-5 h-5" />
          <span className="text-[10px] font-medium mt-0.5">Stock</span>
        </button>

        {usuarioActual.rol === 'superusuario' && (
          <button
            onClick={() => setVistaActual('auditoria')}
            className={`flex flex-col items-center transition ${
              vistaActual === 'auditoria' ? 'text-[#2563eb]' : 'text-zinc-500 hover:text-zinc-300'
            }`}
          >
            <ShieldCheck className="w-5 h-5" />
            <span className="text-[10px] font-medium mt-0.5">Auditoría</span>
          </button>
        )}

        {usuarioActual.rol === 'superusuario' && (
          <button
            onClick={() => setVistaActual('reportes')}
            className={`flex flex-col items-center transition ${
              vistaActual === 'reportes' ? 'text-[#2563eb]' : 'text-zinc-500 hover:text-zinc-300'
            }`}
          >
            <BarChart3 className="w-5 h-5" />
            <span className="text-[10px] font-medium mt-0.5">Reportes</span>
          </button>
        )}

        {usuarioActual.rol === 'superusuario' && (
          <button
            onClick={() => setVistaActual('config')}
            className={`flex flex-col items-center transition ${
              vistaActual === 'config' ? 'text-[#2563eb]' : 'text-zinc-500 hover:text-zinc-300'
            }`}
          >
            <Settings className="w-5 h-5" />
            <span className="text-[10px] font-medium mt-0.5">Ajustes</span>
          </button>
        )}

        <button
          onClick={() => setVistaActual('deploy')}
          className={`flex flex-col items-center transition ${
            vistaActual === 'deploy' ? 'text-[#2563eb]' : 'text-zinc-500 hover:text-zinc-300'
          }`}
        >
          <FileCode className="w-5 h-5" />
          <span className="text-[10px] font-medium mt-0.5">Render</span>
        </button>
      </nav>

      {/* ============================================================== */}
      {/* MODAL DE IMPRESIÓN Y REPORTE EJECUTIVO PDF                     */}
      {/* ============================================================== */}
      {modalReportePrintAbierto && (
        <div className="fixed inset-0 z-50 bg-black/90 backdrop-blur-md flex items-center justify-center p-2 sm:p-4 overflow-y-auto">
          <div className="bg-white text-zinc-900 w-full max-w-4xl rounded-2xl p-6 sm:p-8 space-y-6  my-auto print:p-0 print:shadow-none print:w-full">
            {/* Cabecera del Documento Imprimible */}
            <div className="flex justify-between items-start border-b-2 border-zinc-900 pb-4">
              <div>
                <div className="flex items-center space-x-2">
                  <div className="w-8 h-8 rounded-lg bg-[#2563eb] flex items-center justify-center text-white font-black text-lg">
                    B
                  </div>
                  <h1 className="text-xl font-black tracking-tight text-zinc-950 uppercase">
                    Burger Admin • Multisede & Auditoría
                  </h1>
                </div>
                <p className="text-xs text-zinc-600 font-semibold mt-1">
                  Informe Financiero Oficial y Rendimiento Operativo
                </p>
              </div>

              <div className="text-right text-xs">
                <div className="font-bold text-zinc-900">
                  Período: {fechaInicioReporte} al {fechaFinReporte}
                </div>
                <div className="text-zinc-500 text-[11px]">
                  Sede: {filtroSedeReporte === 'todas' ? 'Todas las Sedes (Consolidado)' : `Sede ${filtroSedeReporte}`}
                </div>
                <div className="text-zinc-500 text-[11px]">
                  Generado: {new Date().toLocaleDateString()} por {usuarioActual.nombre}
                </div>
              </div>
            </div>

            {/* Resumen de los 3 Bolsillos */}
            <div className="grid grid-cols-3 gap-3">
              <div className="border border-zinc-300 p-3 rounded-xl bg-zinc-50">
                <span className="text-[10px] uppercase font-bold text-zinc-600 block">
                  Bolsillo 1 (Reposición Intocable)
                </span>
                <span className="text-xl font-black text-blue-700 font-mono">
                  ${reporte1_Ventas.totalCostosBOM.toFixed(2)} USD
                </span>
              </div>
              <div className="border border-zinc-300 p-3 rounded-xl bg-zinc-50">
                <span className="text-[10px] uppercase font-bold text-zinc-600 block">
                  Bolsillo 2 (Reserva Operativa)
                </span>
                <span className="text-xl font-black text-amber-700 font-mono">
                  ${((reporte1_Ventas.totalUSD - reporte1_Ventas.totalCostosBOM) * (pctReservaOperativa / 100) + gastosOperativosUSD).toFixed(2)} USD
                </span>
              </div>
              <div className="border border-zinc-300 p-3 rounded-xl bg-zinc-50">
                <span className="text-[10px] uppercase font-bold text-zinc-600 block">
                  Bolsillo 3 (Utilidad Neta Libre)
                </span>
                <span className="text-xl font-black text-emerald-700 font-mono">
                  ${Math.max(0, (reporte1_Ventas.totalUSD - reporte1_Ventas.totalCostosBOM) * (1 - pctReservaOperativa / 100) - gastosOperativosUSD).toFixed(2)} USD
                </span>
              </div>
            </div>

            {/* Tabla del Reporte Seleccionado */}
            <div className="space-y-2">
              <h3 className="text-sm font-black text-zinc-900 uppercase tracking-wider">
                {reporteSubTab === 'ventas' && 'Rendimiento de Ventas Diarias'}
                {reporteSubTab === 'inventario' && 'Rotación y Consumo de Insumos (BOM)'}
                {reporteSubTab === 'rentabilidad' && 'Rentabilidad y Margen por Producto'}
                {reporteSubTab === 'cajeros' && 'Productividad y Cuadre de Cajeros'}
              </h3>

              <table className="w-full text-left text-xs border border-zinc-300">
                <thead className="bg-zinc-100 text-zinc-700 font-bold uppercase text-[10px] border-b border-zinc-300">
                  {reporteSubTab === 'ventas' && (
                    <tr>
                      <th className="p-2">Fecha</th>
                      <th className="p-2 text-right">Órdenes</th>
                      <th className="p-2 text-right">Total Facturado USD</th>
                      <th className="p-2 text-right">Equivalente Oficial Bs</th>
                    </tr>
                  )}
                  {reporteSubTab === 'inventario' && (
                    <tr>
                      <th className="p-2">Insumo</th>
                      <th className="p-2">Unidad</th>
                      <th className="p-2 text-right">Consumo BOM</th>
                      <th className="p-2 text-right">Stock Actual</th>
                      <th className="p-2 text-right">Valor Consumido USD</th>
                    </tr>
                  )}
                  {reporteSubTab === 'rentabilidad' && (
                    <tr>
                      <th className="p-2">Producto</th>
                      <th className="p-2 text-right">Vendidos</th>
                      <th className="p-2 text-right">Ingresos USD</th>
                      <th className="p-2 text-right">Costo Insumos</th>
                      <th className="p-2 text-right">Margen %</th>
                    </tr>
                  )}
                  {reporteSubTab === 'cajeros' && (
                    <tr>
                      <th className="p-2">Cajero</th>
                      <th className="p-2">Sede</th>
                      <th className="p-2 text-right">Órdenes</th>
                      <th className="p-2 text-right">Total USD</th>
                      <th className="p-2 text-right">Ticket Promedio</th>
                    </tr>
                  )}
                </thead>
                <tbody className="divide-y divide-zinc-200">
                  {reporteSubTab === 'ventas' &&
                    reporte1_Ventas.listaDias.map(d => (
                      <tr key={d.fecha}>
                        <td className="p-2 font-mono">{d.fecha}</td>
                        <td className="p-2 text-right font-mono">{d.ordenes}</td>
                        <td className="p-2 text-right font-mono font-bold">${d.totalUSD.toFixed(2)}</td>
                        <td className="p-2 text-right font-mono">Bs {(d.totalUSD * tasaActiva).toFixed(2)}</td>
                      </tr>
                    ))}
                  {reporteSubTab === 'inventario' &&
                    reporte2_Inventario.tablaInsumos.map(i => (
                      <tr key={i.id}>
                        <td className="p-2 font-bold">{i.nombre}</td>
                        <td className="p-2 text-zinc-600">{i.unidad}</td>
                        <td className="p-2 text-right font-mono">{i.consumo}</td>
                        <td className="p-2 text-right font-mono">{i.stockActual}</td>
                        <td className="p-2 text-right font-mono font-bold">${i.valorConsumidoUSD.toFixed(2)}</td>
                      </tr>
                    ))}
                  {reporteSubTab === 'rentabilidad' &&
                    reporte3_Rentabilidad.tablaProductos.map(p => (
                      <tr key={p.producto.id}>
                        <td className="p-2 font-bold">{p.producto.nombre}</td>
                        <td className="p-2 text-right font-mono">{p.cant}</td>
                        <td className="p-2 text-right font-mono font-bold">${p.ingresosUSD.toFixed(2)}</td>
                        <td className="p-2 text-right font-mono">${p.costoBOM.toFixed(2)}</td>
                        <td className="p-2 text-right font-mono font-bold text-blue-700">{p.margenPct.toFixed(1)}%</td>
                      </tr>
                    ))}
                  {reporteSubTab === 'cajeros' &&
                    reporte4_Cajeros.map(c => (
                      <tr key={c.id}>
                        <td className="p-2 font-bold">{c.nombre}</td>
                        <td className="p-2">{c.sedeNombre}</td>
                        <td className="p-2 text-right font-mono">{c.ordenesProcesadas}</td>
                        <td className="p-2 text-right font-mono font-bold">${c.totalUSD.toFixed(2)}</td>
                        <td className="p-2 text-right font-mono">${c.ticketPromedio.toFixed(2)}</td>
                      </tr>
                    ))}
                </tbody>
              </table>
            </div>

            {/* Firmas de Auditoría */}
            <div className="grid grid-cols-2 gap-8 pt-8 border-t border-zinc-300 text-xs text-center">
              <div>
                <div className="border-t border-zinc-400 w-48 mx-auto mb-1"></div>
                <span className="font-bold text-zinc-800">Superusuario / Propietario</span>
                <span className="text-[10px] text-zinc-500 block">Firma y Sello de Auditoría</span>
              </div>
              <div>
                <div className="border-t border-zinc-400 w-48 mx-auto mb-1"></div>
                <span className="font-bold text-zinc-800">Gerencia de Operaciones</span>
                <span className="text-[10px] text-zinc-500 block">Conforme Arqueo y Stock</span>
              </div>
            </div>

            {/* Botones de Acción (Ocultos en impresión) */}
            <div className="flex justify-end space-x-3 pt-4 border-t border-zinc-200 print:hidden">
              <button
                onClick={() => setModalReportePrintAbierto(false)}
                className="px-4 py-2 border border-zinc-300 rounded-xl text-xs font-bold text-zinc-700 hover:bg-zinc-100"
              >
                Cerrar
              </button>
              <button
                onClick={dispararImpresionPDF}
                className="px-5 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-xl text-xs font-bold flex items-center space-x-2"
              >
                <Printer className="w-4 h-4" />
                <span>Imprimir / Guardar como PDF</span>
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ============================================================== */}
      {/* MODAL DE COBRO INTELIGENTE & VUELTOS CRUZADOS                   */}
      {/* ============================================================== */}
      {modalCobroAbierto && (
        <div className="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-end sm:items-center justify-center p-0 sm:p-4 print:hidden">
          <div className="bg-[#111114] border border-[#222227] w-full max-w-lg rounded-t-3xl sm:rounded-2xl p-5 space-y-4 max-h-[90vh] overflow-y-auto">
            <div className="flex justify-between items-center pb-2 border-b border-[#222227]">
              <div className="flex items-center space-x-2">
                <CreditCard className="w-4 h-4 text-[#2563eb]" />
                <h3 className="font-bold text-sm text-white">Cobro Inteligente & Vueltos Cruzados</h3>
              </div>
              <button
                onClick={() => setModalCobroAbierto(false)}
                className="text-zinc-400 hover:text-white p-1 text-sm font-bold"
              >
                ✕
              </button>
            </div>

            <div className="bg-[#09090b] p-3 rounded-xl border border-[#222227] flex justify-between items-center">
              <div>
                <span className="text-[10px] text-zinc-400 uppercase font-bold">Total a Pagar</span>
                <div className="text-2xl font-black text-white font-mono">
                  ${totalCarritoUSD.toFixed(2)}
                </div>
              </div>
              <div className="text-right">
                <span className="text-[10px] text-zinc-400 uppercase font-bold">Equivalente Oficial</span>
                <div className="text-sm font-mono font-bold text-[#60a5fa]">
                  Bs {totalCarritoBS.toFixed(2)}
                </div>
              </div>
            </div>

            <div className="space-y-3 text-xs">
              <div className="text-[11px] font-bold text-zinc-300 uppercase tracking-wider">
                1. Dinero Recibido del Cliente
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-zinc-400 mb-1">Efectivo USD (Billetes Físicos)</label>
                  <input
                    type="number"
                    step="0.5"
                    value={cobroRecibidoUSD}
                    onChange={e => setCobroRecibidoUSD(parseFloat(e.target.value) || 0)}
                    className="w-full bg-[#09090b] border border-[#222227] rounded-xl p-2.5 font-mono font-bold text-white focus:border-[#2563eb] outline-none"
                  />
                </div>
                <div>
                  <label className="block text-zinc-400 mb-1">Pago Móvil / Punto (Bs)</label>
                  <input
                    type="number"
                    step="1"
                    value={cobroRecibidoBS}
                    onChange={e => setCobroRecibidoBS(parseFloat(e.target.value) || 0)}
                    className="w-full bg-[#09090b] border border-[#222227] rounded-xl p-2.5 font-mono font-bold text-white focus:border-[#2563eb] outline-none"
                  />
                </div>
              </div>

              <div className="text-[11px] font-bold text-zinc-300 uppercase tracking-wider pt-1">
                2. Cambio / Vueltos Entregados
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-zinc-400 mb-1">Vuelto en Billetes (USD)</label>
                  <input
                    type="number"
                    step="0.5"
                    value={cobroVueltoUSD}
                    onChange={e => setCobroVueltoUSD(parseFloat(e.target.value) || 0)}
                    className="w-full bg-[#09090b] border border-[#222227] rounded-xl p-2.5 font-mono font-bold text-white focus:border-[#2563eb] outline-none"
                  />
                </div>
                <div>
                  <label className="block text-zinc-400 mb-1">Vuelto Cruzado (Bs Móvil)</label>
                  <input
                    type="number"
                    step="1"
                    value={cobroVueltoBS}
                    onChange={e => setCobroVueltoBS(parseFloat(e.target.value) || 0)}
                    className="w-full bg-[#09090b] border border-[#222227] rounded-xl p-2.5 font-mono font-bold text-white focus:border-[#2563eb] outline-none"
                  />
                </div>
              </div>
            </div>

            <div
              className={`p-3 rounded-xl border text-xs font-bold text-center ${
                Math.abs(diferenciaPago) < 0.05
                  ? 'bg-emerald-950/40 border-emerald-800 text-emerald-400'
                  : diferenciaPago > 0
                  ? 'bg-amber-950/40 border-amber-800 text-amber-400'
                  : 'bg-red-950/40 border-red-800 text-red-400'
              }`}
            >
              {Math.abs(diferenciaPago) < 0.05
                ? '✓ Cuadre Exacto: Pago Completo'
                : diferenciaPago > 0
                ? `Sobrante del Cliente: +$${diferenciaPago.toFixed(2)} USD (Entrega vuelto)`
                : `Falta por Cobrar: $${Math.abs(diferenciaPago).toFixed(2)} USD`}
            </div>

            
            {/* Módulo QR y Envío Rápido por WhatsApp al Cliente */}
            <div className="bg-[#09090b] border border-[#222227] rounded-xl p-3 space-y-2 text-xs">
              <div className="flex items-center justify-between">
                <span className="font-bold text-zinc-300 flex items-center space-x-1.5">
                  <QrCode className="w-4 h-4 text-[#2563eb]" />
                  <span>Datos Pago Móvil / QR ({metodosQR[0]?.banco || 'Banesco'})</span>
                </span>
                <button
                  type="button"
                  onClick={() => setMostrarQRModal(!mostrarQRModal)}
                  className="text-[#60a5fa] font-bold hover:underline"
                >
                  {mostrarQRModal ? 'Ocultar QR' : 'Ver QR en Pantalla'}
                </button>
              </div>
              <div className="font-mono text-[11px] text-zinc-300 bg-[#111114] p-2 rounded-lg border border-[#222227] flex justify-between items-center">
                <span>{metodosQR[0]?.banco} | Tel: {metodosQR[0]?.telefono} | CI: {metodosQR[0]?.rif_cedula}</span>
                <button
                  type="button"
                  onClick={() => {
                    navigator.clipboard.writeText(`${metodosQR[0]?.banco} ${metodosQR[0]?.telefono} ${metodosQR[0]?.rif_cedula} Monto: Bs ${totalCarritoBS.toFixed(2)}`);
                    alert('Datos y monto en Bs copiados al portapapeles.');
                  }}
                  className="text-xs bg-zinc-800 px-2 py-1 rounded text-white hover:bg-zinc-700"
                >
                  Copiar
                </button>
              </div>
              {mostrarQRModal && (
                <div className="p-3 bg-white text-black rounded-xl text-center space-y-2">
                  <div className="font-black text-base">Monto Exacto: Bs {totalCarritoBS.toFixed(2)}</div>
                  {metodosQR[0]?.qr_base64 ? (
                    <img src={metodosQR[0].qr_base64} alt="QR Banco" className="w-44 h-44 mx-auto object-contain rounded-lg border" />
                  ) : (
                    <div className="w-40 h-40 mx-auto border-2 border-dashed border-zinc-400 rounded-lg flex items-center justify-center text-xs text-zinc-500 p-2">
                      Sube la imagen de tu QR bancario en la pestaña Ajustes
                    </div>
                  )}
                  <div className="text-xs font-bold">{metodosQR[0]?.banco} • {metodosQR[0]?.telefono} • {metodosQR[0]?.rif_cedula}</div>
                </div>
              )}
              <button
                type="button"
                onClick={() => {
                  const msg = `🌭 *Pedido Burger Admin*\nTotal a pagar: *$${totalCarritoUSD.toFixed(2)}* (*Bs ${totalCarritoBS.toFixed(2)}*)\n\n📲 *Datos Pago Móvil:*\nBanco: ${metodosQR[0]?.banco}\nTeléfono: ${metodosQR[0]?.telefono}\nCédula/RIF: ${metodosQR[0]?.rif_cedula}`;
                  window.open(`https://wa.me/${clienteTelefono.replace(/[^0-9]/g, '')}?text=${encodeURIComponent(msg)}`, '_blank');
                }}
                className="w-full py-2 bg-emerald-700 hover:bg-emerald-600 text-white font-bold rounded-xl flex items-center justify-center space-x-1.5 transition"
              >
                <Share2 className="w-3.5 h-3.5" />
                <span>Enviar Cobro y Datos por WhatsApp al Cliente</span>
              </button>
            </div>

            <button
              onClick={confirmarVenta}
              disabled={!esFiado && diferenciaPago < -0.05}
              className="w-full py-3 bg-[#2563eb] hover:bg-[#1d4ed8] disabled:opacity-40 disabled:cursor-not-allowed text-white font-bold text-sm rounded-xl transition"
            >
              Confirmar Cobro & Descontar Receta (BOM)
            </button>
          </div>
        </div>
      )}

      {/* ============================================================== */}
      {/* MODAL DE CAMBIO DE USUARIO                                     */}
      {/* ============================================================== */}
      {modalLoginAbierto && (
        <div className="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-center justify-center p-4 print:hidden">
          <div className="bg-[#111114] border border-[#222227] w-full max-w-sm rounded-2xl p-5 space-y-4 max-h-[90vh] overflow-y-auto">
            <div className="flex justify-between items-center pb-2 border-b border-[#222227]">
              <h3 className="font-bold text-sm text-white">Mi cuenta</h3>
              <button onClick={() => { setModalLoginAbierto(false); setPinMsg(''); }} className="text-zinc-400 hover:text-white">
                ✕
              </button>
            </div>

            <div className="bg-[#09090b] border border-[#222227] rounded-xl p-3">
              <div className="font-bold text-xs text-white">{usuarioActual.nombre}</div>
              <div className="text-[10px] text-zinc-400 uppercase font-semibold">
                {usuarioActual.rol} • {usuarioActual.sede_id ? `Sede ${usuarioActual.sede_id}` : 'Acceso Global'}
              </div>
            </div>

            <div className="space-y-2 text-xs">
              <div className="text-[11px] font-bold text-zinc-300 uppercase tracking-wider">Cambiar mi PIN</div>
              <input type="password" inputMode="numeric" placeholder="PIN actual" value={pinActual} onChange={e => setPinActual(e.target.value)}
                className="w-full bg-[#09090b] border border-[#222227] rounded-xl p-2.5 text-white font-mono outline-none focus:border-[#2563eb]" />
              <input type="password" inputMode="numeric" placeholder="PIN nuevo (6 a 12 dígitos)" value={pinNuevo} onChange={e => setPinNuevo(e.target.value)}
                className="w-full bg-[#09090b] border border-[#222227] rounded-xl p-2.5 text-white font-mono outline-none focus:border-[#2563eb]" />
              <input type="password" inputMode="numeric" placeholder="Repite el PIN nuevo" value={pinRepetir} onChange={e => setPinRepetir(e.target.value)}
                className="w-full bg-[#09090b] border border-[#222227] rounded-xl p-2.5 text-white font-mono outline-none focus:border-[#2563eb]" />
              <button onClick={cambiarMiPin} className="w-full py-2.5 bg-[#2563eb] hover:bg-[#1d4ed8] text-white font-bold rounded-xl">
                Guardar PIN nuevo
              </button>
            </div>

            {usuarioActual.rol === 'superusuario' && (
              <div className="space-y-2 text-xs pt-2 border-t border-[#222227]">
                <div className="text-[11px] font-bold text-zinc-300 uppercase tracking-wider">Restablecer PIN de otro usuario</div>
                <select value={pinResetUser} onChange={e => setPinResetUser(e.target.value)}
                  className="w-full bg-[#09090b] border border-[#222227] rounded-xl p-2.5 text-white">
                  <option value="">Elige un usuario…</option>
                  {usuariosSistema.filter(u => u.id !== usuarioActual.id).map(u => (
                    <option key={u.id} value={u.id}>{u.nombre} ({u.rol})</option>
                  ))}
                </select>
                <input type="password" inputMode="numeric" placeholder="PIN nuevo (6 a 12 dígitos)" value={pinResetNuevo} onChange={e => setPinResetNuevo(e.target.value)}
                  className="w-full bg-[#09090b] border border-[#222227] rounded-xl p-2.5 text-white font-mono outline-none focus:border-[#2563eb]" />
                <button onClick={restablecerPin} className="w-full py-2.5 bg-zinc-800 hover:bg-zinc-700 text-white font-bold rounded-xl">
                  Restablecer PIN
                </button>
              </div>
            )}

            {pinMsg && <div className="text-[11px] font-bold text-amber-400 text-center">{pinMsg}</div>}

            <button
              onClick={() => { setModalLoginAbierto(false); onLogout(); }}
              className="w-full py-2.5 bg-red-600 hover:bg-red-500 text-white font-bold text-xs rounded-xl transition"
            >
              Cerrar sesión
            </button>
          </div>
        </div>
      )}

      {/* ============================================================== */}
      {/* MODAL DE AUDITORÍA: ANULAR ORDEN                               */}
      {/* ============================================================== */}
      {modalAuditoriaAbierto && ordenParaAuditar && (
        <div className="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-center justify-center p-4 print:hidden">
          <div className="bg-[#111114] border border-red-900/60 w-full max-w-md rounded-2xl p-5 space-y-4">
            <div className="flex justify-between items-center pb-2 border-b border-[#222227]">
              <div className="flex items-center space-x-2 text-red-400 font-bold text-sm">
                <AlertTriangle className="w-4 h-4" />
                <span>Auditoría: Anulación de Orden #{ordenParaAuditar.id}</span>
              </div>
              <button onClick={() => setModalAuditoriaAbierto(false)} className="text-zinc-400 hover:text-white">
                ✕
              </button>
            </div>

            <div>
              <label className="block text-xs font-bold text-zinc-300 mb-1">
                Justificación Obligatoria de Auditoría
              </label>
              <textarea
                rows={3}
                placeholder="Indica el motivo exacto de la anulación..."
                value={justificacionAuditoria}
                onChange={e => setJustificacionAuditoria(e.target.value)}
                className="w-full bg-[#09090b] border border-[#222227] rounded-xl p-2.5 text-xs text-white focus:border-red-500 outline-none"
              ></textarea>
            </div>

            <div className="flex space-x-2">
              <button
                onClick={() => setModalAuditoriaAbierto(false)}
                className="w-1/2 py-2.5 bg-zinc-800 text-zinc-300 font-bold text-xs rounded-xl"
              >
                Cancelar
              </button>
              <button
                onClick={anularOrdenAuditoria}
                className="w-1/2 py-2.5 bg-red-600 hover:bg-red-500 text-white font-bold text-xs rounded-xl transition"
              >
                Confirmar Anulación
              </button>
            </div>
          </div>
        </div>
      )}

      {/* MODAL DE MOVIMIENTO DE INVENTARIO (entrada / ajuste / transferencia) */}
      {modalMovAbierto && (
        <div className="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-center justify-center p-4 print:hidden">
          <div className="bg-[#111114] border border-[#222227] w-full max-w-sm rounded-2xl p-5 space-y-4 max-h-[90vh] overflow-y-auto">
            <div className="flex justify-between items-center pb-2 border-b border-[#222227]">
              <h3 className="font-bold text-sm text-white">Movimiento de inventario</h3>
              <button onClick={() => setModalMovAbierto(false)} className="text-zinc-400 hover:text-white">✕</button>
            </div>

            <div className="grid grid-cols-3 gap-1.5 text-[11px] font-bold">
              {[['COMPRA', 'Entrada'], ['AJUSTE', 'Ajustar conteo'], ['TRANSFERENCIA', 'Transferir']].map(([t, label]) => (
                <button
                  key={t}
                  onClick={() => setMovForm(f => ({ ...f, tipo: t }))}
                  className={`py-2 rounded-lg border transition ${movForm.tipo === t ? 'bg-[#2563eb] border-[#2563eb] text-white' : 'bg-[#09090b] border-[#222227] text-zinc-300'}`}
                >
                  {label}
                </button>
              ))}
            </div>

            <div className="space-y-3 text-xs">
              <div className="text-[11px] text-zinc-400">
                Sede: <span className="font-bold text-white">{sedeStockActual ? sedeStockActual.nombre : ''}</span>
              </div>

              <div>
                <label className="text-zinc-400 block mb-1">Insumo</label>
                <select
                  value={movForm.insumo_id}
                  onChange={e => setMovForm(f => ({ ...f, insumo_id: e.target.value }))}
                  className="w-full bg-[#09090b] border border-[#222227] rounded-xl p-2.5 text-white"
                >
                  {insumos.map(i => (
                    <option key={i.id} value={i.id}>{i.nombre} ({i.unidad})</option>
                  ))}
                </select>
                {insumoMov && (
                  <div className="text-[10px] text-zinc-500 mt-1">
                    Stock actual aquí: {inventarioSedes[`${sedeStockId}_${insumoMov.id}`] ?? 0} {insumoMov.unidad} • 1 paquete = {insumoMov.rendimiento_paquete} {insumoMov.unidad}
                  </div>
                )}
              </div>

              {movForm.tipo === 'AJUSTE' ? (
                <div>
                  <label className="text-zinc-400 block mb-1">Conteo real (lo que hay físicamente)</label>
                  <input
                    type="number" min="0" step="any"
                    value={movForm.nuevo_stock}
                    onChange={e => setMovForm(f => ({ ...f, nuevo_stock: e.target.value }))}
                    className="w-full bg-[#09090b] border border-[#222227] rounded-xl p-2.5 text-white font-mono font-bold"
                  />
                </div>
              ) : (
                <div>
                  <label className="text-zinc-400 block mb-1">Cantidad</label>
                  <div className="flex gap-2">
                    <input
                      type="number" min="0" step="any"
                      value={movForm.cantidad}
                      onChange={e => setMovForm(f => ({ ...f, cantidad: e.target.value }))}
                      className="flex-1 bg-[#09090b] border border-[#222227] rounded-xl p-2.5 text-white font-mono font-bold"
                    />
                    <select
                      value={movForm.unidadEntrada}
                      onChange={e => setMovForm(f => ({ ...f, unidadEntrada: e.target.value }))}
                      className="bg-[#09090b] border border-[#222227] rounded-xl p-2.5 text-white"
                    >
                      <option value="unidad">unidades</option>
                      <option value="paquete">paquetes</option>
                    </select>
                  </div>
                </div>
              )}

              {movForm.tipo === 'TRANSFERENCIA' && (
                <div>
                  <label className="text-zinc-400 block mb-1">Sede destino</label>
                  <select
                    value={movForm.sede_destino_id}
                    onChange={e => setMovForm(f => ({ ...f, sede_destino_id: e.target.value }))}
                    className="w-full bg-[#09090b] border border-[#222227] rounded-xl p-2.5 text-white"
                  >
                    <option value="">Elige una sede…</option>
                    {sedes.filter(s => s.id !== sedeStockId).map(s => (
                      <option key={s.id} value={s.id}>{s.nombre}</option>
                    ))}
                  </select>
                </div>
              )}

              {movForm.tipo === 'COMPRA' && (
                <div>
                  <label className="text-zinc-400 block mb-1">Nuevo costo del paquete en USD (opcional)</label>
                  <input
                    type="number" min="0" step="any"
                    value={movForm.costo_paquete_usd}
                    onChange={e => setMovForm(f => ({ ...f, costo_paquete_usd: e.target.value }))}
                    placeholder={insumoMov ? `Actual: ${insumoMov.costo_paquete_usd}` : ''}
                    className="w-full bg-[#09090b] border border-[#222227] rounded-xl p-2.5 text-white font-mono"
                  />
                </div>
              )}

              <div>
                <label className="text-zinc-400 block mb-1">
                  {movForm.tipo === 'AJUSTE' ? 'Motivo (obligatorio)' : 'Nota (opcional)'}
                </label>
                <input
                  type="text"
                  value={movForm.motivo}
                  onChange={e => setMovForm(f => ({ ...f, motivo: e.target.value }))}
                  className="w-full bg-[#09090b] border border-[#222227] rounded-xl p-2.5 text-white"
                />
              </div>

              <button
                disabled={guardando}
                onClick={enviarMovimiento}
                className="w-full py-2.5 bg-[#2563eb] hover:bg-[#1d4ed8] disabled:opacity-50 font-bold text-white rounded-xl"
              >
                {guardando ? 'Guardando…' : 'Registrar movimiento'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* MODAL DE INSUMO (crear / editar) */}
      {modalInsumoAbierto && insumoForm && (
        <div className="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-center justify-center p-4 print:hidden">
          <div className="bg-[#111114] border border-[#222227] w-full max-w-sm rounded-2xl p-5 space-y-4 max-h-[90vh] overflow-y-auto">
            <div className="flex justify-between items-center pb-2 border-b border-[#222227]">
              <h3 className="font-bold text-sm text-white">{insumoForm.id ? 'Editar insumo' : 'Nuevo insumo'}</h3>
              <button onClick={() => setModalInsumoAbierto(false)} className="text-zinc-400 hover:text-white">✕</button>
            </div>

            <div className="space-y-3 text-xs">
              <div>
                <label className="text-zinc-400 block mb-1">Nombre</label>
                <input type="text" value={insumoForm.nombre}
                  onChange={e => setInsumoForm(f => ({ ...f, nombre: e.target.value }))}
                  className="w-full bg-[#09090b] border border-[#222227] rounded-xl p-2.5 text-white" />
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="text-zinc-400 block mb-1">Unidad</label>
                  <input type="text" value={insumoForm.unidad}
                    onChange={e => setInsumoForm(f => ({ ...f, unidad: e.target.value }))}
                    className="w-full bg-[#09090b] border border-[#222227] rounded-xl p-2.5 text-white" />
                </div>
                <div>
                  <label className="text-zinc-400 block mb-1">Alerta de stock mínimo</label>
                  <input type="number" min="0" step="any" value={insumoForm.alerta_stock_minimo}
                    onChange={e => setInsumoForm(f => ({ ...f, alerta_stock_minimo: e.target.value }))}
                    className="w-full bg-[#09090b] border border-[#222227] rounded-xl p-2.5 text-white font-mono" />
                </div>
                <div>
                  <label className="text-zinc-400 block mb-1">Costo del paquete (USD)</label>
                  <input type="number" min="0" step="any" value={insumoForm.costo_paquete_usd}
                    onChange={e => setInsumoForm(f => ({ ...f, costo_paquete_usd: e.target.value }))}
                    className="w-full bg-[#09090b] border border-[#222227] rounded-xl p-2.5 text-white font-mono" />
                </div>
                <div>
                  <label className="text-zinc-400 block mb-1">Unidades por paquete</label>
                  <input type="number" min="0" step="any" value={insumoForm.rendimiento_paquete}
                    onChange={e => setInsumoForm(f => ({ ...f, rendimiento_paquete: e.target.value }))}
                    className="w-full bg-[#09090b] border border-[#222227] rounded-xl p-2.5 text-white font-mono" />
                </div>
              </div>
              {Number(insumoForm.rendimiento_paquete) > 0 && insumoForm.costo_paquete_usd !== '' && (
                <div className="text-[10px] text-zinc-500">
                  Costo por unidad: ${(Number(insumoForm.costo_paquete_usd) / Number(insumoForm.rendimiento_paquete)).toFixed(4)}
                </div>
              )}
              {!insumoForm.id && (
                <div>
                  <label className="text-zinc-400 block mb-1">
                    Stock inicial en {sedeStockActual ? sedeStockActual.nombre : 'la sede'} (opcional)
                  </label>
                  <input type="number" min="0" step="any" value={insumoForm.stock_inicial}
                    onChange={e => setInsumoForm(f => ({ ...f, stock_inicial: e.target.value }))}
                    className="w-full bg-[#09090b] border border-[#222227] rounded-xl p-2.5 text-white font-mono" />
                </div>
              )}
              <button
                disabled={guardando}
                onClick={guardarInsumo}
                className="w-full py-2.5 bg-[#2563eb] hover:bg-[#1d4ed8] disabled:opacity-50 font-bold text-white rounded-xl"
              >
                {guardando ? 'Guardando…' : 'Guardar insumo'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* MODAL DE MERMA */}
      {modalMermaAbierto && (
        <div className="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-center justify-center p-4 print:hidden">
          <div className="bg-[#111114] border border-[#222227] w-full max-w-sm rounded-2xl p-5 space-y-4">
            <div className="flex justify-between items-center pb-2 border-b border-[#222227]">
              <h3 className="font-bold text-sm text-white">Reportar Merma o Gasto Menor</h3>
              <button onClick={() => setModalMermaAbierto(false)} className="text-zinc-400 hover:text-white">
                ✕
              </button>
            </div>

            <div className="space-y-3 text-xs">
              <div>
                <label className="text-zinc-400 block mb-1">Insumo afectado</label>
                <select
                  id="select-merma-insumo"
                  className="w-full bg-[#09090b] border border-[#222227] rounded-xl p-2.5 text-white"
                >
                  {insumos.map(i => (
                    <option key={i.id} value={i.id}>
                      {i.nombre} ({i.unidad})
                    </option>
                  ))}
                </select>
              </div>

              <div>
                <label className="text-zinc-400 block mb-1">Cantidad de Merma</label>
                <input
                  type="number"
                  id="input-merma-cant"
                  defaultValue={1}
                  className="w-full bg-[#09090b] border border-[#222227] rounded-xl p-2.5 text-white font-bold"
                />
              </div>

              <button
                onClick={async () => {
                  const insumoId = Number(document.getElementById('select-merma-insumo').value);
                  const cant = Number(document.getElementById('input-merma-cant').value);
                  if (!(cant > 0)) { alert('Escribe una cantidad mayor a 0.'); return; }
                  try {
                    const res = await fetch('/api/inventario/movimiento', {
                      method: 'POST',
                      headers: { 'Content-Type': 'application/json' },
                      body: JSON.stringify({
                        sede_id: sedeStockId,
                        insumo_id: insumoId,
                        tipo: 'MERMA',
                        cantidad: cant,
                        motivo: 'Merma reportada en puesto'
                      })
                    });
                    const data = await res.json();
                    if (!data.success) { alert(data.error || 'No se pudo registrar la merma.'); return; }
                    setModalMermaAbierto(false);
                    await cargarDesdeBackend();
                  } catch (err) { alert('Sin conexión con el servidor.'); }
                }}
                className="w-full py-2.5 bg-red-600 hover:bg-red-500 font-bold text-white rounded-xl"
              >
                Registrar Merma en Stock
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function PantallaLogin({ onOk }) {
  const [username, setUsername] = useState('');
  const [pin, setPin] = useState('');
  const [error, setError] = useState('');
  const [cargando, setCargando] = useState(false);

  const entrar = async (e) => {
    e.preventDefault();
    if (!username.trim() || !pin) { setError('Escribe tu usuario y tu PIN.'); return; }
    setCargando(true);
    setError('');
    try {
      const res = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: username.trim(), pin })
      });
      const data = await res.json();
      if (data.success) {
        onOk(data.user);
      } else {
        setError(data.error || 'No se pudo iniciar sesión.');
        setPin('');
      }
    } catch (err) {
      setError('Sin conexión con el servidor.');
    }
    setCargando(false);
  };

  return (
    <div className="min-h-screen bg-[#09090b] flex items-center justify-center p-4">
      <form onSubmit={entrar} className="bg-[#111114] border border-[#222227] w-full max-w-sm rounded-2xl p-6 space-y-4">
        <div className="text-center space-y-1">
          <h1 className="text-lg font-black text-white tracking-tight">Doggi POS</h1>
          <p className="text-xs text-zinc-400">Inicia sesión para continuar</p>
        </div>
        <div className="space-y-3 text-xs">
          <div>
            <label className="text-zinc-400 block mb-1">Usuario</label>
            <input
              type="text"
              autoComplete="username"
              autoCapitalize="none"
              value={username}
              onChange={e => setUsername(e.target.value)}
              className="w-full bg-[#09090b] border border-[#222227] rounded-xl p-3 text-white outline-none focus:border-[#2563eb]"
            />
          </div>
          <div>
            <label className="text-zinc-400 block mb-1">PIN</label>
            <input
              type="password"
              inputMode="numeric"
              autoComplete="current-password"
              value={pin}
              onChange={e => setPin(e.target.value)}
              className="w-full bg-[#09090b] border border-[#222227] rounded-xl p-3 text-white font-mono tracking-widest outline-none focus:border-[#2563eb]"
            />
          </div>
        </div>
        {error && <div className="text-xs font-bold text-red-400 text-center">{error}</div>}
        <button
          type="submit"
          disabled={cargando}
          className="w-full py-3 bg-[#2563eb] hover:bg-[#1d4ed8] disabled:opacity-50 text-white font-bold text-sm rounded-xl transition"
        >
          {cargando ? 'Entrando…' : 'Entrar'}
        </button>
      </form>
    </div>
  );
}

function App() {
  const [usuario, setUsuario] = useState(null);
  const [verificando, setVerificando] = useState(true);

  useEffect(() => {
    fetch('/api/auth/me')
      .then(r => (r.ok ? r.json() : null))
      .then(d => { if (d && d.success) setUsuario(d.user); })
      .catch(() => {})
      .finally(() => setVerificando(false));
    const alExpirar = () => setUsuario(null);
    window.addEventListener('sesion-expirada', alExpirar);
    return () => window.removeEventListener('sesion-expirada', alExpirar);
  }, []);

  const cerrarSesion = async () => {
    try { await fetch('/api/auth/logout', { method: 'POST' }); } catch (e) {}
    setUsuario(null);
  };

  if (verificando) {
    return <div className="min-h-screen bg-[#09090b] flex items-center justify-center text-zinc-400 text-sm">Cargando…</div>;
  }
  if (!usuario) return <PantallaLogin onOk={setUsuario} />;
  return <AppInner key={usuario.id} usuarioInicial={usuario} onLogout={cerrarSesion} />;
}

    const root = ReactDOM.createRoot(document.getElementById('root'));
    root.render(<App />);
  </script>
</body>
</html>
"""

@app.after_request
def cabeceras_seguridad(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/", methods=["GET"])
def index():
    return app.response_class(response=HTML_TEMPLATE, status=200, mimetype="text/html")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    print(f">> Iniciando Burger Admin Unificado en http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
