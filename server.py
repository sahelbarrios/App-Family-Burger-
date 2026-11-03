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
if os.environ.get("RENDER"):
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

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

if os.environ.get("RENDER") and not USE_POSTGRES:
    print(">> [PELIGRO] Estás en Render SIN base de datos Postgres: los datos se borran en cada reinicio. Define DATABASE_URL.")

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
            # CAST(...) funciona igual si la columna es boolean o integer
            sql = sql.replace("activa = 1", "CAST(activa AS INTEGER) = 1")
            sql = sql.replace("activo = 1", "CAST(activo AS INTEGER) = 1")
            sql = sql.replace("es_almacen = 1", "CAST(es_almacen AS INTEGER) = 1")
            sql = sql.replace("es_almacen = 0", "CAST(es_almacen AS INTEGER) = 0")
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
FLAG_TRUE, FLAG_FALSE = (True, False) if USE_POSTGRES else (1, 0)


def pin_valido(pin):
    return bool(re.fullmatch(r"\d{6,12}", str(pin or "")))


def _pin_inicial(env_name):
    pin = os.environ.get(env_name, "").strip()
    if not pin_valido(pin):
        pin = "".join(secrets.choice("0123456789") for _ in range(6))
        print(f">> [SEGURIDAD] PIN inicial generado para {env_name}: {pin}  (cámbialo apenas entres)")
    return pin


# ----------------------------------------------------------------------
# HORA LOCAL (Venezuela, UTC-4 fijo, sin horario de verano) Y MIGRACIONES
# ----------------------------------------------------------------------
TZ_LOCAL = datetime.timezone(datetime.timedelta(hours=-4))


def ahora_utc_str():
    """Marca de tiempo que se guarda en la BD (siempre UTC)."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def a_local(raw):
    """Convierte lo guardado en la BD (UTC) a hora de Venezuela. Devuelve None si no se puede leer."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime.datetime):
        dt = raw
    else:
        s = str(raw).strip().replace("T", " ")
        try:
            dt = datetime.datetime.fromisoformat(s)
        except ValueError:
            try:
                dt = datetime.datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(TZ_LOCAL)


def hoy_local():
    return datetime.datetime.now(TZ_LOCAL).date()


def _migraciones_admin(db):
    """Tablas y columnas nuevas del panel de administración. Es seguro correrlo en cada arranque."""
    ddl = [
        """CREATE TABLE IF NOT EXISTS proveedores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL,
            telefono TEXT,
            notas TEXT,
            vigente INTEGER NOT NULL DEFAULT 1,
            created_at TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS compras (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            proveedor_id INTEGER NOT NULL,
            sede_id INTEGER,
            descripcion TEXT,
            total_usd NUMERIC(12, 2) NOT NULL,
            pagado_usd NUMERIC(12, 2) NOT NULL DEFAULT 0,
            estado TEXT NOT NULL DEFAULT 'PENDIENTE',
            fecha TEXT NOT NULL,
            usuario_id INTEGER,
            anulada INTEGER NOT NULL DEFAULT 0,
            created_at TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS pagos_compra (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            compra_id INTEGER NOT NULL,
            monto_usd NUMERIC(12, 2) NOT NULL,
            metodo TEXT,
            nota TEXT,
            fecha TEXT NOT NULL,
            usuario_id INTEGER,
            created_at TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS gastos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sede_id INTEGER,
            categoria TEXT NOT NULL,
            descripcion TEXT,
            monto_usd NUMERIC(12, 2) NOT NULL,
            metodo TEXT,
            proveedor_id INTEGER,
            fecha TEXT NOT NULL,
            usuario_id INTEGER,
            anulado INTEGER NOT NULL DEFAULT 0,
            anulado_motivo TEXT,
            created_at TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS combo_componentes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            combo_id INTEGER NOT NULL,
            producto_id INTEGER NOT NULL,
            cantidad INTEGER NOT NULL DEFAULT 1,
            UNIQUE(combo_id, producto_id)
        )""",
    ]
    for t in ddl:
        db.execute(t)
    db.commit()

    for tabla, col in [("ordenes", "cobrado_at"), ("ordenes", "metodo_cobro")]:
        try:
            db.execute(f"SELECT {col} FROM {tabla} LIMIT 1")
            db.commit()
        except Exception:
            db.conn.rollback()
            tipo = "TIMESTAMPTZ" if (USE_POSTGRES and col.endswith("_at")) else "TEXT"
            try:
                db.execute(f"ALTER TABLE {tabla} ADD COLUMN {col} {tipo}")
                db.commit()
            except Exception as e:
                db.conn.rollback()
                print(f">> [WARN] No se pudo agregar {tabla}.{col}: {e}")

    if USE_POSTGRES:
        try:
            db.execute("ALTER TABLE auditoria_logs DROP CONSTRAINT IF EXISTS auditoria_logs_accion_check")
            db.execute("ALTER TABLE auditoria_logs ADD CONSTRAINT auditoria_logs_accion_check CHECK (accion IN ("
                       "'ANULAR_ORDEN','EDITAR_ORDEN','AJUSTE_STOCK','CAMBIO_PRECIO','MERMA_DECLARADA','TRANSFERENCIA',"
                       "'GESTION_USUARIO','GESTION_SEDE','EDITAR_PRODUCTO','GASTO','COMPRA_PROVEEDOR','PAGO_PROVEEDOR'))")
            db.commit()
        except Exception as e:
            db.conn.rollback()
            print(f">> [WARN] No se pudo ampliar el constraint de auditoría ({e}).")


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
    _migraciones_admin(db)

    # El kardex debe aceptar el tipo AJUSTE (conteo físico).
    global KARDEX_ACEPTA_AJUSTE, FLAG_TRUE, FLAG_FALSE
    if USE_POSTGRES:
        try:
            tipo_flag = db.execute("SELECT data_type FROM information_schema.columns WHERE table_name = 'usuarios' AND column_name = 'activo'").fetchone()
            if tipo_flag and tipo_flag["data_type"] != "boolean":
                FLAG_TRUE, FLAG_FALSE = 1, 0
        except Exception as e:
            db.conn.rollback()
            print(f">> [WARN] No se pudo detectar el tipo de la columna activo ({e}).")
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

    row_prods = d