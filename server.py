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
import io
import base64
import urllib.parse
import requests
from functools import wraps
from flask import Flask, request, jsonify, render_template_string, session, g, abort
from werkzeug.security import generate_password_hash, check_password_hash
from itsdangerous import URLSafeSerializer, BadSignature

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
    MAX_CONTENT_LENGTH=12 * 1024 * 1024,  # fotos de comprobantes (se comprimen antes de ir a Drive)
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


# ----------------------------------------------------------------------
# GOOGLE DRIVE (comprobantes y facturas) + FACTURA DIGITAL EN BOLÍVARES
# En la BD solo se guarda el id del archivo; la imagen vive en tu carpeta de Drive.
# Variables de entorno: DRIVE_SCRIPT_URL y DRIVE_SCRIPT_TOKEN (el mini script de Google Apps Script)
# Datos de la factura:  NEGOCIO_NOMBRE, NEGOCIO_RIF, NEGOCIO_TEL, NEGOCIO_DIRECCION, FACTURA_PREFIJO
# ----------------------------------------------------------------------
NEGOCIO_NOMBRE = os.environ.get("NEGOCIO_NOMBRE", "Family Burger")
NEGOCIO_RIF = os.environ.get("NEGOCIO_RIF", "J-00000000-0")
NEGOCIO_TEL = os.environ.get("NEGOCIO_TEL", "")
NEGOCIO_DIRECCION = os.environ.get("NEGOCIO_DIRECCION", "")
FACTURA_PREFIJO = os.environ.get("FACTURA_PREFIJO", "FB")
ENTIDADES_ARCHIVO = {"orden": "ordenes", "compra": "compras", "pago_compra": "pagos_compra", "gasto": "gastos"}
_firmador_factura = URLSafeSerializer(app.secret_key, salt="factura-publica")


def drive_configurado():
    return all(os.environ.get(v, "").strip() for v in ("DRIVE_SCRIPT_URL", "DRIVE_SCRIPT_TOKEN"))


def _drive_llamar(payload):
    """Habla con el mini script de Apps Script que vive en tu Drive (sin APIs ni credenciales)."""
    payload["token"] = os.environ["DRIVE_SCRIPT_TOKEN"].strip()
    r = requests.post(os.environ["DRIVE_SCRIPT_URL"].strip(), data=json.dumps(payload),
                      headers={"Content-Type": "text/plain"}, timeout=45)
    r.raise_for_status()
    d = r.json()
    if not d.get("ok"):
        raise RuntimeError(d.get("error", "error desconocido en el script de Drive"))
    return d


def drive_subir(nombre, data, mime):
    d = _drive_llamar({"accion": "subir", "nombre": nombre, "mime": mime, "data": base64.b64encode(data).decode()})
    return d["id"]


def drive_descargar(file_id):
    d = _drive_llamar({"accion": "leer", "id": file_id})
    return base64.b64decode(d["data"])


def comprimir_imagen(data, max_px=1280, calidad=72):
    """Una foto de celular (3-6 MB) queda en ~100-250 KB. Devuelve (bytes, mime)."""
    from PIL import Image, ImageOps
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    img.thumbnail((max_px, max_px))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=calidad, optimize=True)
    return out.getvalue(), "image/jpeg"


def fmt_bs(v):
    """1234.5 -> 'Bs. 1.234,50'"""
    t = f"{float(v or 0):,.2f}"
    return "Bs. " + t.replace(",", "X").replace(".", ",").replace("X", ".")


def normalizar_telefono_ve(tel):
    d = re.sub(r"\D", "", tel or "")
    if d.startswith("00"):
        d = d[2:]
    if not d or d.startswith("58"):
        return d
    if d.startswith("0"):
        d = d[1:]
    return "58" + d


def _mods_texto(raw):
    try:
        lista = json.loads(raw or "[]")
    except Exception:
        return []
    return [str(m) for m in lista if str(m).strip()] if isinstance(lista, list) else []


PLANTILLA_FACTURA = r"""<!doctype html>
<html lang="es"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Factura {{ numero }} - {{ negocio }}</title>
<style>
  *{box-sizing:border-box}
  body{margin:0;background:#f3f1ee;font-family:-apple-system,"Segoe UI",Roboto,Arial,sans-serif;color:#222;padding:16px}
  .hoja{max-width:640px;margin:0 auto;background:#fff;border-radius:14px;overflow:hidden;box-shadow:0 2px 14px rgba(0,0,0,.08);position:relative}
  .top{background:#c8102e;color:#fff;padding:22px 24px;display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}
  .top h1{margin:0 0 4px;font-size:22px;letter-spacing:.3px}
  .top p{margin:0;font-size:12.5px;opacity:.92;line-height:1.5}
  .num{text-align:right}
  .num span{display:block;font-size:11px;letter-spacing:2px;opacity:.85}
  .num strong{display:block;font-size:22px}
  .num small{font-size:12px;opacity:.9}
  .cuerpo{padding:22px 24px}
  .cliente{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;font-size:13.5px;margin-bottom:18px;padding-bottom:14px;border-bottom:1px dashed #d8d4cf}
  .cliente b{display:block;font-size:11px;color:#8a847d;text-transform:uppercase;letter-spacing:1px;margin-bottom:2px}
  table{width:100%;border-collapse:collapse;font-size:13.5px}
  th{text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:1px;color:#8a847d;padding:8px 0;border-bottom:2px solid #222}
  td{padding:10px 0;border-bottom:1px solid #eee;vertical-align:top}
  .r{text-align:right;white-space:nowrap}
  .mod{display:block;font-size:11.5px;color:#8a847d}
  .totales{margin-top:14px;margin-left:auto;max-width:300px;font-size:14px}
  .totales div{display:flex;justify-content:space-between;padding:5px 0}
  .totales .total{border-top:2px solid #222;margin-top:6px;padding-top:10px;font-size:19px;font-weight:700}
  .pago{margin-top:18px;background:#faf8f6;border-radius:10px;padding:12px 14px;font-size:13px;line-height:1.7}
  .pie{text-align:center;padding:18px 24px 24px;font-size:12.5px;color:#8a847d}
  .pie strong{color:#222;display:block;font-size:14px;margin-bottom:2px}
  .btn{display:block;max-width:640px;margin:14px auto 0;text-align:center}
  .btn button{background:#222;color:#fff;border:0;border-radius:10px;padding:12px 22px;font-size:14px;cursor:pointer}
  .anulada{position:absolute;top:42%;left:50%;transform:translate(-50%,-50%) rotate(-18deg);font-size:64px;font-weight:800;color:rgba(200,16,46,.22);border:6px solid rgba(200,16,46,.22);padding:4px 24px;border-radius:12px}
  @media print{body{background:#fff;padding:0}.hoja{box-shadow:none;border-radius:0}.btn{display:none}}
</style></head><body>
<div class="hoja">
  {% if anulada %}<div class="anulada">ANULADA</div>{% endif %}
  <div class="top">
    <div>
      <h1>{{ negocio }}</h1>
      <p>RIF: {{ rif }}{% if tel %} · Tel: {{ tel }}{% endif %}{% if direccion %}<br>{{ direccion }}{% endif %}{% if sede %}<br>{{ sede }}{% endif %}</p>
    </div>
    <div class="num"><span>FACTURA</span><strong>{{ numero }}</strong><small>{{ fecha }}</small></div>
  </div>
  <div class="cuerpo">
    <div class="cliente">
      <div><b>Cliente</b>{{ cliente or "Consumidor final" }}{% if cliente_tel %}<br>{{ cliente_tel }}{% endif %}</div>
      <div><b>Atendido por</b>{{ cajero or "-" }}</div>
    </div>
    <table>
      <thead><tr><th>Cant.</th><th>Descripción</th><th class="r">P. unit.</th><th class="r">Total</th></tr></thead>
      <tbody>
      {% for it in lineas %}
        <tr>
          <td>{{ it.cantidad }}</td>
          <td>{{ it.nombre }}{% for m in it.mods %}<span class="mod">+ {{ m }}</span>{% endfor %}</td>
          <td class="r">{{ it.unit }}</td>
          <td class="r">{{ it.total }}</td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
    <div class="totales">
      <div class="total"><span>TOTAL</span><span>{{ total }}</span></div>
      {% if pagado %}<div><span>Pagado</span><span>{{ pagado }}</span></div>{% endif %}
      {% if vuelto %}<div><span>Vuelto</span><span>{{ vuelto }}</span></div>{% endif %}
    </div>
    {% if fiado %}<div class="pago"><b>Estado:</b> Pendiente de pago (crédito)<br><b>Saldo a pagar:</b> {{ total }}</div>{% endif %}
    {% if referencia %}<div class="pago"><b>Forma de pago:</b> Pago electrónico<br><b>Referencia:</b> {{ referencia }}</div>{% endif %}
  </div>
  <div class="pie"><strong>¡Gracias por tu compra!</strong>Todos los montos están expresados en bolívares (Bs.)</div>
</div>
<div class="btn"><button onclick="window.print()">Imprimir / Guardar PDF</button></div>
</body></html>"""


def _datos_factura(db, oid):
    """Arma todo lo que necesita la factura, con montos SIEMPRE en bolívares (precio USD x tasa de esa venta)."""
    o = db.execute(
        "SELECT o.*, s.nombre AS sede_nombre, u.nombre AS cajero_nombre "
        "FROM ordenes o LEFT JOIN sedes s ON s.id = o.sede_id LEFT JOIN usuarios u ON u.id = o.cajero_id "
        "WHERE o.id = ?", (oid,)).fetchone()
    if not o:
        return None
    o = dict(o)
    filas = db.execute(
        "SELECT i.cantidad, i.precio_unitario_usd, i.modificadores_json, p.nombre "
        "FROM orden_items i LEFT JOIN catalogo_productos p ON p.id = i.producto_id "
        "WHERE i.orden_id = ? ORDER BY i.id", (oid,)).fetchall()
    tasa = float(o["tasa_aplicada"] or 0)
    lineas = []
    for r in filas:
        r = dict(r)
        unit = round(float(r["precio_unitario_usd"]) * tasa, 2)
        lineas.append({"cantidad": int(r["cantidad"]), "nombre": r["nombre"] or "Producto",
                       "mods": _mods_texto(r["modificadores_json"]),
                       "unit": fmt_bs(unit), "total": fmt_bs(round(unit * int(r["cantidad"]), 2))})
    pagado = float(o["recibido_bs"] or 0) + float(o["recibido_usd"] or 0) * tasa
    vuelto = float(o["vuelto_bs"] or 0) + float(o["vuelto_usd"] or 0) * tasa
    estado = str(o.get("estado_pago") or "").upper()
    dt = a_local(o.get("created_at"))
    return {
        "negocio": NEGOCIO_NOMBRE, "rif": NEGOCIO_RIF, "tel": NEGOCIO_TEL, "direccion": NEGOCIO_DIRECCION,
        "sede": o.get("sede_nombre"),
        "numero": f"{FACTURA_PREFIJO}-{int(o['id']):06d}",
        "fecha": dt.strftime("%d/%m/%Y %I:%M %p") if dt else "",
        "cliente": o.get("cliente_nombre"), "cliente_tel": o.get("cliente_telefono"),
        "cajero": o.get("cajero_nombre"), "lineas": lineas,
        "total": fmt_bs(round(float(o["total_venta_usd"]) * tasa, 2)),
        "pagado": fmt_bs(pagado) if pagado > 0.004 else "",
        "vuelto": fmt_bs(vuelto) if vuelto > 0.004 else "",
        "referencia": o.get("referencia_pago") or "",
        "anulada": estado == "ANULADA", "fiado": estado == "FIADO",
        "_telefono": o.get("cliente_telefono") or "",
    }


def _migraciones_pedidos(db):
    """Columnas para los pedidos que hace el público desde el menú (mercancía apartada con vencimiento)."""
    try:
        db.execute("""CREATE TABLE IF NOT EXISTS categorias_gasto (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL,
            activa INTEGER NOT NULL DEFAULT 1
        )""")
        db.commit()
    except Exception as e:
        db.conn.rollback()
        print(f">> [WARN] No se pudo crear categorias_gasto: {e}")
    for col, tipo in [("origen", "TEXT"), ("estado_web", "TEXT"), ("vencimiento", "TEXT"), ("pago_metodo", "TEXT"),
                      ("pago_monto_bs", "DOUBLE PRECISION"), ("pago_reportado_at", "TEXT"), ("ip_creacion", "TEXT")]:
        try:
            db.execute(f"SELECT {col} FROM ordenes LIMIT 1")
            db.commit()
        except Exception:
            db.conn.rollback()
            try:
                db.execute(f"ALTER TABLE ordenes ADD COLUMN {col} {tipo}")
                db.commit()
            except Exception as e:
                db.conn.rollback()
                print(f">> [WARN] No se pudo agregar ordenes.{col}: {e}")


def _crear_usuario_web():
    """Las órdenes necesitan un 'cajero'. Los pedidos web usan este usuario interno, que nunca puede iniciar sesión."""
    db = get_db()
    try:
        if not db.execute("SELECT id FROM usuarios WHERE username = 'pedido_web'").fetchone():
            db.execute("INSERT INTO usuarios (nombre, username, pin_plain, pin_hash, rol, sede_id, activo) VALUES (?, ?, ?, ?, ?, ?, ?)",
                       ("Pedido web", "pedido_web", "", generate_password_hash(secrets.token_hex(24)), "superusuario", None, FLAG_FALSE))
            db.commit()
    except Exception as e:
        db.conn.rollback()
        print(f">> [WARN] No se pudo crear el usuario interno de pedidos web: {e}")
    finally:
        db.close()


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
        """CREATE TABLE IF NOT EXISTS archivos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entidad TEXT NOT NULL,
            entidad_id INTEGER NOT NULL,
            drive_file_id TEXT NOT NULL,
            nombre TEXT,
            mime TEXT,
            tamano INTEGER,
            usuario_id INTEGER,
            created_at TEXT
        )""",
    ]
    for t in ddl:
        db.execute(t)
    db.commit()

    for tabla, col in [("ordenes", "cobrado_at"), ("ordenes", "metodo_cobro"), ("ordenes", "referencia_pago")]:
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
    _migraciones_pedidos(db)

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
    _crear_usuario_web()
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
    ip = request.remote_addr or ""
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


def _texto_a_float(v):
    """Acepta 12.5 y también 12,5 (teclados en español). Si trae ambos, el último separador es el decimal."""
    if isinstance(v, str):
        limpio = re.sub(r"[^0-9.,\-]", "", v)
        i = max(limpio.rfind(","), limpio.rfind("."))
        if i >= 0:
            limpio = limpio[:i].replace(",", "").replace(".", "") + "." + limpio[i + 1:].replace(",", "").replace(".", "")
        return float(limpio)
    return float(v)


_TIPOS_FLAG = {}


def _valor_flag(db, tabla, columna, valor):
    """Escribe verdadero/falso según el tipo REAL de la columna en tu base (boolean o integer)."""
    if not USE_POSTGRES:
        return 1 if valor else 0
    clave = (tabla, columna)
    if clave not in _TIPOS_FLAG:
        r = db.execute("SELECT data_type FROM information_schema.columns WHERE table_name = ? AND column_name = ?", (tabla, columna)).fetchone()
        _TIPOS_FLAG[clave] = (r is None) or r["data_type"] == "boolean"
    return bool(valor) if _TIPOS_FLAG[clave] else (1 if valor else 0)


def _num(v, nombre, minimo=None):
    try:
        x = _texto_a_float(v)
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
        ordenes_rows = [dict(o) for o in db.execute("SELECT * FROM ordenes WHERE (estado_web IS NULL OR estado_web = 'CONFIRMADA') ORDER BY id DESC LIMIT 5000").fetchall()]
        items_rows = [dict(it) for it in db.execute(
            "SELECT oi.* FROM orden_items oi JOIN (SELECT id FROM ordenes ORDER BY id DESC LIMIT 5000) o ON o.id = oi.orden_id").fetchall()]
    else:
        ordenes_rows = [dict(o) for o in db.execute("SELECT * FROM ordenes WHERE sede_id = ? AND (estado_web IS NULL OR estado_web = 'CONFIRMADA') ORDER BY id DESC LIMIT 1500", (g.user["sede_id"],)).fetchall()]
        items_rows = [dict(it) for it in db.execute(
            "SELECT oi.* FROM orden_items oi JOIN (SELECT id FROM ordenes WHERE sede_id = ? ORDER BY id DESC LIMIT 1500) o ON o.id = oi.orden_id",
            (g.user["sede_id"],)).fetchall()]
    todos_nombres = {u["id"]: u["nombre"] for u in db.execute("SELECT id, nombre FROM usuarios").fetchall()}
    logs_rows = [dict(l) for l in db.execute("SELECT * FROM auditoria_logs ORDER BY id DESC LIMIT 50").fetchall()] if es_super else []
    archivos_por_orden = {}
    if ordenes_rows:
        desde_id = min(o["id"] for o in ordenes_rows)
        for a in db.execute("SELECT id, entidad_id FROM archivos WHERE entidad = 'orden' AND entidad_id >= ? ORDER BY id", (desde_id,)).fetchall():
            archivos_por_orden.setdefault(a["entidad_id"], []).append(a["id"])
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

    user_map = todos_nombres
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
        loc = a_local(o.get("created_at")) or datetime.datetime.now(TZ_LOCAL)
        fecha_corta = loc.strftime("%Y-%m-%d")
        hora_corta = loc.strftime("%H:%M")
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
            "referencia_pago": o.get("referencia_pago") or "",
            "archivos": archivos_por_orden.get(o["id"], []),
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
    try:
        return _tasa_sync_interno()
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": f"No se pudieron guardar las tasas ({type(e).__name__}: {str(e)[:150]})"}), 500


def _tasa_sync_interno():
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
        u_man = _valor_flag(db, "configuracion", "usar_tasa_manual", usar)
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
    recibido_usd = _f(data.get("recibido_usd", 0))
    recibido_bs = _f(data.get("recibido_bs", 0))
    vuelto_usd = _f(data.get("vuelto_usd", 0))
    vuelto_bs = _f(data.get("vuelto_bs", 0))
    estado_pago = data.get("estado_pago", "PAGADA")
    if estado_pago not in ("PAGADA", "FIADO"):
        estado_pago = "PAGADA"
    cliente_nombre = data.get("cliente_nombre", "Consumidor")
    cliente_telefono = data.get("cliente_telefono", "")
    notas_cocina = data.get("notas_cocina", "")
    referencia_pago = str(data.get("referencia_pago", "") or "").strip()[:60]
    if estado_pago == "FIADO":
        recibido_usd = recibido_bs = vuelto_usd = vuelto_bs = 0.0
        if len(str(cliente_nombre or "").strip()) < 2 or str(cliente_nombre).strip().lower() in ("consumidor", "cliente fiado"):
            return jsonify({"success": False, "error": "Para vender fiado escribe el nombre del cliente."}), 400

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
        try:
            p_id = int(it["producto_id"])
            cant = int(it.get("cantidad", 1))
        except (KeyError, TypeError, ValueError):
            continue
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

    ahora_str = ahora_utc_str()
    orden_id = db.insert_returning_id("""
        INSERT INTO ordenes (
            sede_id, cajero_id, cliente_nombre, cliente_telefono,
            total_venta_usd, costo_insumos_usd, tasa_aplicada, tasa_reposicion_momento,
            recibido_usd, recibido_bs, vuelto_usd, vuelto_bs,
            estado_pago, estado_cocina, notas_cocina, referencia_pago, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PREPARANDO', ?, ?, ?)
    """, (
        sede_id, cajero_id, cliente_nombre, cliente_telefono,
        total_venta_usd, costo_total_insumos_usd, tasa_aplicada, tasa_reposicion,
        recibido_usd, recibido_bs, vuelto_usd, vuelto_bs,
        estado_pago, notas_cocina, referencia_pago, ahora_str
    ))

    for p_id, cant, p_precio, c_unit, mods_json in items_para_insertar:
        db.execute("""
            INSERT INTO orden_items (orden_id, producto_id, cantidad, precio_unitario_usd, costo_unitario_usd, modificadores_json)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (orden_id, p_id, cant, p_precio, c_unit, mods_json))

    for iid, cant_desc in desglose_insumos.items():
        ajustar_stock(db, sede_id, iid, -cant_desc, permitir_negativo=True)
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
        db.execute("UPDATE ordenes SET estado_pago = ?, cobrado_at = ?, metodo_cobro = ? WHERE id = ?",
                   (estado_pago, ahora_utc_str(), str(data.get("metodo_cobro", ""))[:40], orden_id))
    db.commit()
    db.close()
    return jsonify({"success": True})

@app.route("/api/auditoria/corregir", methods=["POST"])
@requiere_login(roles=("superusuario",))
def api_auditoria_corregir():
    data = request.get_json(silent=True) or {}
    try:
        orden_id = int(data.get("orden_id"))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "Orden no válida."}), 400
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

    ahora_str = ahora_utc_str()
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
        ahora = ahora_utc_str()
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
    verdadero = FLAG_TRUE
    falso = FLAG_FALSE
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
                        (sede_ini, iid, g.user["id"], stock_ini, cu, "Stock inicial al crear el insumo", ahora_utc_str()))
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


# ======================================================================
# PANEL DE ADMINISTRACIÓN (solo superusuario): usuarios, sedes, catálogo y finanzas
# ======================================================================
CATEGORIAS_GASTO = ["Alquiler", "Servicios (luz, agua, internet)", "Sueldos", "Transporte / Delivery",
                    "Gas", "Mantenimiento", "Publicidad", "Impuestos / Permisos", "Otros"]
def _categorias_gasto(db):
    """Categorías de fábrica + las que agregó el dueño. Devuelve (todas, solo_propias)."""
    propias = [r["nombre"] for r in db.execute("SELECT nombre FROM categorias_gasto WHERE activa = 1 ORDER BY nombre").fetchall()]
    base = list(CATEGORIAS_GASTO)
    ya = {c.lower() for c in base}
    propias = [c for c in propias if c.lower() not in ya]
    return base + propias, propias


def _registrar_categoria(db, nombre):
    nombre = " ".join(str(nombre or "").split())[:40]
    if len(nombre) < 2:
        raise ValueError("El nombre de la categoría es muy corto.")
    todas, propias = _categorias_gasto(db)
    for c in todas:
        if c.lower() == nombre.lower():
            return c
    if len(propias) >= 60:
        raise ValueError("Ya tienes muchas categorías propias. Quita alguna que no uses.")
    existe = db.execute("SELECT id FROM categorias_gasto WHERE LOWER(nombre) = ?", (nombre.lower(),)).fetchone()
    if existe:
        db.execute("UPDATE categorias_gasto SET activa = ? WHERE id = ?", (1, existe["id"]))
    else:
        db.execute("INSERT INTO categorias_gasto (nombre, activa) VALUES (?, ?)", (nombre, 1))
    return nombre


METODOS_PAGO = ["Efectivo USD", "Efectivo Bs", "Pago móvil", "Transferencia", "Zelle", "Otro"]
TIPOS_PRODUCTO = ("INDIVIDUAL", "COMBO", "BEBIDA", "EXTRA")


def admin_api(fn):
    """Abre la BD, confirma al terminar y convierte los errores en respuestas JSON claras."""
    @wraps(fn)
    def wrapper(*a, **kw):
        db = get_db()
        try:
            res = fn(db, *a, **kw)
            db.commit()
            return res
        except ValueError as e:
            db.conn.rollback()
            return jsonify({"success": False, "error": str(e)}), 400
        except Exception as e:
            db.conn.rollback()
            print(f">> [ERROR] {fn.__name__}: {e}")
            return jsonify({"success": False, "error": "Error interno al procesar la solicitud."}), 500
        finally:
            db.close()
    return wrapper


def _json_body():
    return request.get_json(silent=True) or {}


def _entero(v, nombre, obligatorio=True):
    if v in (None, ""):
        if obligatorio:
            raise ValueError(f"{nombre} es obligatorio.")
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        raise ValueError(f"{nombre} no es válido.")


def _fecha(v, defecto=None):
    s = str(v or "").strip()[:10]
    if not s:
        return defecto or hoy_local().isoformat()
    try:
        datetime.date.fromisoformat(s)
    except ValueError:
        raise ValueError("Fecha no válida (usa AAAA-MM-DD).")
    return s


def _f(v):
    """float tolerante: texto o nulo se vuelve 0."""
    try:
        x = _texto_a_float(v)
        return x if x == x and abs(x) != float("inf") else 0.0
    except (TypeError, ValueError):
        return 0.0


def log_auditoria(db, accion, entidad, registro_id, antes, despues, justificacion, sede_id=None):
    db.execute(
        "INSERT INTO auditoria_logs (superusuario_id, sede_id, accion, entidad_afectada, registro_id, "
        "snapshot_anterior_json, snapshot_nuevo_json, justificacion, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (g.user["id"], sede_id, accion, entidad, registro_id,
         json.dumps(antes, default=str) if antes is not None else None,
         json.dumps(despues, default=str) if despues is not None else None,
         (justificacion or "").strip() or "Cambio desde el panel de administración", ahora_utc_str()))


# ----------------------------------------------------------------------
# USUARIOS
# ----------------------------------------------------------------------
def _usuario_pub(u):
    return {"id": u["id"], "nombre": u["nombre"], "username": u["username"], "rol": u["rol"],
            "sede_id": u["sede_id"], "activo": bool(u["activo"])}


def _superusuarios_activos(db):
    return int(db.execute("SELECT COUNT(*) AS c FROM usuarios WHERE rol = 'superusuario' AND activo = 1").fetchone()["c"])


@app.route("/api/admin/usuarios", methods=["GET", "POST"])
@requiere_login(roles=("superusuario",))
@admin_api
def api_admin_usuarios(db):
    if request.method == "GET":
        filas = db.execute("SELECT id, nombre, username, rol, sede_id, activo FROM usuarios ORDER BY id").fetchall()
        return jsonify({"success": True, "usuarios": [_usuario_pub(dict(r)) for r in filas]})

    data = _json_body()
    nombre = str(data.get("nombre", "")).strip()[:120]
    username = str(data.get("username", "")).strip().lower()[:60]
    pin = str(data.get("pin", "")).strip()
    rol = data.get("rol", "cajero")
    sede = _entero(data.get("sede_id"), "La sede", False)
    if rol not in ("cajero", "superusuario"):
        raise ValueError("Rol inválido.")
    if not nombre:
        raise ValueError("El nombre es obligatorio.")
    if not re.fullmatch(r"[a-z0-9._-]{3,60}", username):
        raise ValueError("El usuario debe tener 3 a 60 caracteres: letras, números, punto, guion o guion bajo.")
    if not pin_valido(pin):
        raise ValueError(MSG_PIN)
    if rol == "cajero":
        if not sede:
            raise ValueError("Un cajero necesita una sede asignada.")
        if not db.execute("SELECT id FROM sedes WHERE id = ?", (sede,)).fetchone():
            raise ValueError("Sede no encontrada.")
    if db.execute("SELECT id FROM usuarios WHERE LOWER(username) = ?", (username,)).fetchone():
        raise ValueError("Ese nombre de usuario ya existe.")
    uid = db.insert_returning_id(
        "INSERT INTO usuarios (nombre, username, pin_plain, pin_hash, rol, sede_id, activo) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (nombre, username, "", generate_password_hash(pin), rol, sede, FLAG_TRUE))
    log_auditoria(db, "GESTION_USUARIO", "usuarios", uid, None,
                  {"nombre": nombre, "username": username, "rol": rol, "sede_id": sede}, "Usuario creado", sede)
    return jsonify({"success": True, "id": uid})


@app.route("/api/admin/usuarios/<int:uid>", methods=["PATCH"])
@requiere_login(roles=("superusuario",))
@admin_api
def api_admin_usuario_editar(db, uid):
    data = _json_body()
    fila = db.execute("SELECT * FROM usuarios WHERE id = ?", (uid,)).fetchone()
    if not fila:
        raise ValueError("Usuario no encontrado.")
    antes = _usuario_pub(dict(fila))
    nuevo = dict(antes)

    if "nombre" in data:
        nuevo["nombre"] = str(data["nombre"]).strip()[:120]
        if not nuevo["nombre"]:
            raise ValueError("El nombre no puede quedar vacío.")
    if "username" in data:
        un = str(data["username"]).strip().lower()[:60]
        if not re.fullmatch(r"[a-z0-9._-]{3,60}", un):
            raise ValueError("El usuario debe tener 3 a 60 caracteres: letras, números, punto, guion o guion bajo.")
        if db.execute("SELECT id FROM usuarios WHERE LOWER(username) = ? AND id <> ?", (un, uid)).fetchone():
            raise ValueError("Ese nombre de usuario ya existe.")
        nuevo["username"] = un
    if "rol" in data:
        if data["rol"] not in ("cajero", "superusuario"):
            raise ValueError("Rol inválido.")
        nuevo["rol"] = data["rol"]
    if "sede_id" in data:
        nuevo["sede_id"] = _entero(data["sede_id"], "La sede", False)
    if "activo" in data:
        nuevo["activo"] = bool(data["activo"])

    if nuevo["rol"] == "cajero":
        if not nuevo["sede_id"]:
            raise ValueError("Un cajero necesita una sede asignada.")
        if not db.execute("SELECT id FROM sedes WHERE id = ?", (nuevo["sede_id"],)).fetchone():
            raise ValueError("Sede no encontrada.")
    if uid == g.user["id"] and (not nuevo["activo"] or nuevo["rol"] != "superusuario"):
        raise ValueError("No puedes desactivarte ni quitarte el rol de superusuario a ti mismo.")
    pin = str(data.get("pin") or "").strip()
    if pin and not pin_valido(pin):
        raise ValueError(MSG_PIN)

    db.execute("UPDATE usuarios SET nombre = ?, username = ?, rol = ?, sede_id = ?, activo = ? WHERE id = ?",
               (nuevo["nombre"], nuevo["username"], nuevo["rol"], nuevo["sede_id"],
                FLAG_TRUE if nuevo["activo"] else FLAG_FALSE, uid))
    if pin:
        db.execute("UPDATE usuarios SET pin_hash = ?, pin_plain = '' WHERE id = ?", (generate_password_hash(pin), uid))
    if _superusuarios_activos(db) < 1:
        raise ValueError("Debe quedar al menos un superusuario activo.")

    despues = dict(nuevo)
    if pin:
        despues["pin_cambiado"] = True
    log_auditoria(db, "GESTION_USUARIO", "usuarios", uid, antes, despues, data.get("justificacion") or "Usuario editado")
    return jsonify({"success": True, "usuario": nuevo})


# ----------------------------------------------------------------------
# SEDES
# ----------------------------------------------------------------------
@app.route("/api/admin/sedes", methods=["GET", "POST"])
@requiere_login(roles=("superusuario",))
@admin_api
def api_admin_sedes(db):
    if request.method == "GET":
        filas = db.execute("SELECT id, nombre, es_almacen, activa FROM sedes ORDER BY id").fetchall()
        return jsonify({"success": True, "sedes": [
            {"id": r["id"], "nombre": r["nombre"], "es_almacen": bool(r["es_almacen"]), "activa": bool(r["activa"])}
            for r in filas]})
    data = _json_body()
    nombre = str(data.get("nombre", "")).strip()[:120]
    if not nombre:
        raise ValueError("El nombre de la sede es obligatorio.")
    sid = db.insert_returning_id("INSERT INTO sedes (nombre, es_almacen, activa) VALUES (?, ?, ?)", (nombre, FLAG_FALSE, FLAG_TRUE))
    for i in db.execute("SELECT id FROM insumos_maestro").fetchall():
        db.execute("INSERT INTO inventario_por_sede (sede_id, insumo_id, stock_actual) VALUES (?, ?, 0)", (sid, i["id"]))
    log_auditoria(db, "GESTION_SEDE", "sedes", sid, None, {"nombre": nombre}, "Sede creada", sid)
    return jsonify({"success": True, "id": sid})


@app.route("/api/admin/sedes/<int:sid>", methods=["PATCH"])
@requiere_login(roles=("superusuario",))
@admin_api
def api_admin_sede_editar(db, sid):
    data = _json_body()
    fila = db.execute("SELECT * FROM sedes WHERE id = ?", (sid,)).fetchone()
    if not fila:
        raise ValueError("Sede no encontrada.")
    antes = {"nombre": fila["nombre"], "activa": bool(fila["activa"])}
    nombre = str(data.get("nombre", antes["nombre"])).strip()[:120]
    activa = bool(data.get("activa", antes["activa"]))
    if not nombre:
        raise ValueError("El nombre no puede quedar vacío.")
    if not activa:
        if bool(fila["es_almacen"]):
            raise ValueError("El almacén central no se puede desactivar.")
        cajeros = db.execute("SELECT nombre FROM usuarios WHERE sede_id = ? AND rol = 'cajero' AND activo = 1", (sid,)).fetchall()
        if cajeros:
            raise ValueError("Esta sede tiene cajeros activos: " + ", ".join(c["nombre"] for c in cajeros) + ". Reasígnalos o desactívalos primero.")
    db.execute("UPDATE sedes SET nombre = ?, activa = ? WHERE id = ?", (nombre, FLAG_TRUE if activa else FLAG_FALSE, sid))
    log_auditoria(db, "GESTION_SEDE", "sedes", sid, antes, {"nombre": nombre, "activa": activa}, data.get("justificacion") or "Sede editada", sid)
    return jsonify({"success": True})


# ----------------------------------------------------------------------
# CATÁLOGO: productos, precios, recetas y combos
# ----------------------------------------------------------------------
def _receta_desde_lista(db, lista):
    acc = {}
    for it in lista or []:
        iid = _entero(it.get("insumo_id"), "El insumo de la receta")
        cant = _num(it.get("cantidad"), "La cantidad del insumo", 0.0001)
        if not db.execute("SELECT id FROM insumos_maestro WHERE id = ?", (iid,)).fetchone():
            raise ValueError("Un insumo de la receta no existe.")
        acc[iid] = acc.get(iid, 0.0) + cant
    return acc


def _receta_desde_componentes(db, comps, pid_actual=None):
    """Suma las recetas de los productos que forman el combo. Devuelve (insumos, {producto: cantidad})."""
    acc, normal = {}, {}
    for c in comps or []:
        cid = _entero(c.get("producto_id"), "El producto del combo")
        cant = int(_num(c.get("cantidad"), "La cantidad del componente", 1))
        if pid_actual is not None and cid == pid_actual:
            raise ValueError("Un combo no puede incluirse a sí mismo.")
        if not db.execute("SELECT id FROM catalogo_productos WHERE id = ?", (cid,)).fetchone():
            raise ValueError("Un producto del combo no existe.")
        if db.execute("SELECT id FROM combo_componentes WHERE combo_id = ?", (cid,)).fetchone():
            raise ValueError("No puedes meter dentro de un combo otro combo armado con componentes.")
        normal[cid] = normal.get(cid, 0) + cant
    for cid, cant in normal.items():
        for r in db.execute("SELECT insumo_id, cantidad_consumida FROM recetas_bom WHERE producto_id = ?", (cid,)).fetchall():
            acc[r["insumo_id"]] = acc.get(r["insumo_id"], 0.0) + float(r["cantidad_consumida"]) * cant
    return acc, normal


def _escribir_receta(db, pid, acc):
    db.execute("DELETE FROM recetas_bom WHERE producto_id = ?", (pid,))
    for iid, cant in acc.items():
        if cant > 0:
            db.execute("INSERT INTO recetas_bom (producto_id, insumo_id, cantidad_consumida) VALUES (?, ?, ?)", (pid, iid, round(cant, 2)))


def _guardar_componentes(db, combo_id, normal):
    db.execute("DELETE FROM combo_componentes WHERE combo_id = ?", (combo_id,))
    for cid, cant in normal.items():
        db.execute("INSERT INTO combo_componentes (combo_id, producto_id, cantidad) VALUES (?, ?, ?)", (combo_id, cid, cant))


def _reexpandir_combos(db, base_id):
    """Si cambia la receta de un producto base, los combos armados con él se recalculan solos."""
    combos = [r["combo_id"] for r in db.execute("SELECT DISTINCT combo_id FROM combo_componentes WHERE producto_id = ?", (base_id,)).fetchall()]
    for cid in combos:
        comps = db.execute("SELECT producto_id, cantidad FROM combo_componentes WHERE combo_id = ?", (cid,)).fetchall()
        acc, _ = _receta_desde_componentes(db, [{"producto_id": c["producto_id"], "cantidad": c["cantidad"]} for c in comps], cid)
        _escribir_receta(db, cid, acc)


def _catalogo_completo(db):
    costos = {r["id"]: float(r["costo_unitario_usd"]) for r in db.execute("SELECT id, costo_unitario_usd FROM insumos_maestro").fetchall()}
    rec_map, comp_map = {}, {}
    for r in db.execute("SELECT producto_id, insumo_id, cantidad_consumida FROM recetas_bom").fetchall():
        rec_map.setdefault(r["producto_id"], []).append({"insumo_id": r["insumo_id"], "cantidad": float(r["cantidad_consumida"])})
    for r in db.execute("SELECT combo_id, producto_id, cantidad FROM combo_componentes").fetchall():
        comp_map.setdefault(r["combo_id"], []).append({"producto_id": r["producto_id"], "cantidad": int(r["cantidad"])})
    out = []
    for p in db.execute("SELECT * FROM catalogo_productos ORDER BY id").fetchall():
        receta = rec_map.get(p["id"], [])
        costo = sum(costos.get(x["insumo_id"], 0.0) * x["cantidad"] for x in receta)
        precio = float(p["precio_venta_usd"])
        out.append({
            "id": p["id"], "nombre": p["nombre"], "tipo": p["tipo"], "precio_venta_usd": precio,
            "activo": bool(p["activo"]), "receta": receta, "componentes": comp_map.get(p["id"], []),
            "costo_usd": round(costo, 4),
            "margen_pct": round((precio - costo) / precio * 100, 1) if precio > 0 else 0.0,
        })
    return out


@app.route("/api/admin/productos", methods=["GET", "POST"])
@requiere_login(roles=("superusuario",))
@admin_api
def api_admin_productos(db):
    if request.method == "GET":
        return jsonify({"success": True, "productos": _catalogo_completo(db), "tipos": list(TIPOS_PRODUCTO)})

    data = _json_body()
    nombre = str(data.get("nombre", "")).strip()[:120]
    tipo = str(data.get("tipo", "")).upper()
    if not nombre:
        raise ValueError("El nombre del producto es obligatorio.")
    if tipo not in TIPOS_PRODUCTO:
        raise ValueError("Tipo de producto inválido.")
    precio = _num(data.get("precio_venta_usd"), "El precio", 0)
    if db.execute("SELECT id FROM catalogo_productos WHERE LOWER(nombre) = ?", (nombre.lower(),)).fetchone():
        raise ValueError("Ya existe un producto con ese nombre.")

    comps = data.get("componentes") or []
    if comps:
        acc, normal = _receta_desde_componentes(db, comps)
    else:
        acc, normal = _receta_desde_lista(db, data.get("receta")), {}
    if not acc:
        raise ValueError("El producto necesita al menos un insumo en su receta (o productos componentes si es un combo).")

    pid = db.insert_returning_id("INSERT INTO catalogo_productos (nombre, tipo, precio_venta_usd, activo) VALUES (?, ?, ?, ?)",
                                 (nombre, tipo, precio, FLAG_TRUE))
    _escribir_receta(db, pid, acc)
    if normal:
        _guardar_componentes(db, pid, normal)
    log_auditoria(db, "EDITAR_PRODUCTO", "catalogo_productos", pid, None,
                  {"nombre": nombre, "tipo": tipo, "precio_venta_usd": precio}, "Producto creado")
    return jsonify({"success": True, "id": pid})


@app.route("/api/admin/productos/<int:pid>", methods=["PATCH"])
@requiere_login(roles=("superusuario",))
@admin_api
def api_admin_producto_editar(db, pid):
    data = _json_body()
    fila = db.execute("SELECT * FROM catalogo_productos WHERE id = ?", (pid,)).fetchone()
    if not fila:
        raise ValueError("Producto no encontrado.")
    antes = {"nombre": fila["nombre"], "tipo": fila["tipo"], "precio_venta_usd": float(fila["precio_venta_usd"]), "activo": bool(fila["activo"])}
    nombre = str(data.get("nombre", antes["nombre"])).strip()[:120]
    tipo = str(data.get("tipo", antes["tipo"])).upper()
    activo = bool(data.get("activo", antes["activo"]))
    precio = _num(data.get("precio_venta_usd", antes["precio_venta_usd"]), "El precio", 0)
    if not nombre:
        raise ValueError("El nombre no puede quedar vacío.")
    if tipo not in TIPOS_PRODUCTO:
        raise ValueError("Tipo de producto inválido.")
    if db.execute("SELECT id FROM catalogo_productos WHERE LOWER(nombre) = ? AND id <> ?", (nombre.lower(), pid)).fetchone():
        raise ValueError("Ya existe otro producto con ese nombre.")

    receta_cambia = False
    if "componentes" in data and data["componentes"]:
        if db.execute("SELECT id FROM combo_componentes WHERE producto_id = ?", (pid,)).fetchone():
            raise ValueError("Este producto forma parte de otro combo, así que no puede armarse con componentes.")
        acc, normal = _receta_desde_componentes(db, data["componentes"], pid)
        if not acc:
            raise ValueError("El combo necesita al menos un componente con receta.")
        _escribir_receta(db, pid, acc)
        _guardar_componentes(db, pid, normal)
        receta_cambia = True
    elif "receta" in data:
        acc = _receta_desde_lista(db, data["receta"])
        if not acc:
            raise ValueError("La receta necesita al menos un insumo.")
        db.execute("DELETE FROM combo_componentes WHERE combo_id = ?", (pid,))
        _escribir_receta(db, pid, acc)
        receta_cambia = True
    elif "componentes" in data:  # lista vacía: deja de ser un combo armado por componentes
        db.execute("DELETE FROM combo_componentes WHERE combo_id = ?", (pid,))

    db.execute("UPDATE catalogo_productos SET nombre = ?, tipo = ?, precio_venta_usd = ?, activo = ? WHERE id = ?",
               (nombre, tipo, precio, FLAG_TRUE if activo else FLAG_FALSE, pid))
    if receta_cambia:
        _reexpandir_combos(db, pid)

    despues = {"nombre": nombre, "tipo": tipo, "precio_venta_usd": precio, "activo": activo}
    just = data.get("justificacion")
    if abs(precio - antes["precio_venta_usd"]) > 1e-9:
        log_auditoria(db, "CAMBIO_PRECIO", "catalogo_productos", pid,
                      {"precio_venta_usd": antes["precio_venta_usd"]}, {"precio_venta_usd": precio},
                      just or f"Precio de {nombre}: {antes['precio_venta_usd']:.2f} → {precio:.2f}")
    if receta_cambia or any(antes[k] != despues[k] for k in ("nombre", "tipo", "activo")):
        log_auditoria(db, "EDITAR_PRODUCTO", "catalogo_productos", pid, antes, despues,
                      just or ("Receta modificada" if receta_cambia else "Producto editado"))
    return jsonify({"success": True})


# ----------------------------------------------------------------------
# FINANZAS: proveedores, compras (cuentas por pagar), gastos y flujo de caja
# ----------------------------------------------------------------------
def _estado_compra(total, pagado):
    if pagado >= total - 0.004:
        return "PAGADA"
    return "PARCIAL" if pagado > 0.004 else "PENDIENTE"


@app.route("/api/admin/proveedores", methods=["GET", "POST"])
@requiere_login(roles=("superusuario",))
@admin_api
def api_admin_proveedores(db):
    if request.method == "GET":
        deuda = {}
        for r in db.execute("SELECT proveedor_id, total_usd, pagado_usd FROM compras WHERE anulada = 0").fetchall():
            deuda[r["proveedor_id"]] = deuda.get(r["proveedor_id"], 0.0) + float(r["total_usd"]) - float(r["pagado_usd"])
        filas = db.execute("SELECT id, nombre, telefono, notas, vigente FROM proveedores ORDER BY nombre").fetchall()
        return jsonify({"success": True, "proveedores": [
            {"id": r["id"], "nombre": r["nombre"], "telefono": r["telefono"] or "", "notas": r["notas"] or "",
             "vigente": bool(r["vigente"]), "saldo_usd": round(max(0.0, deuda.get(r["id"], 0.0)), 2)} for r in filas]})

    data = _json_body()
    nombre = str(data.get("nombre", "")).strip()[:120]
    if not nombre:
        raise ValueError("El nombre del proveedor es obligatorio.")
    if db.execute("SELECT id FROM proveedores WHERE LOWER(nombre) = ?", (nombre.lower(),)).fetchone():
        raise ValueError("Ya existe un proveedor con ese nombre.")
    pid = db.insert_returning_id("INSERT INTO proveedores (nombre, telefono, notas, vigente, created_at) VALUES (?, ?, ?, 1, ?)",
                                 (nombre, str(data.get("telefono", ""))[:40], str(data.get("notas", ""))[:300], ahora_utc_str()))
    return jsonify({"success": True, "id": pid})


@app.route("/api/admin/proveedores/<int:pid>", methods=["PATCH"])
@requiere_login(roles=("superusuario",))
@admin_api
def api_admin_proveedor_editar(db, pid):
    data = _json_body()
    fila = db.execute("SELECT * FROM proveedores WHERE id = ?", (pid,)).fetchone()
    if not fila:
        raise ValueError("Proveedor no encontrado.")
    nombre = str(data.get("nombre", fila["nombre"])).strip()[:120]
    if not nombre:
        raise ValueError("El nombre no puede quedar vacío.")
    if db.execute("SELECT id FROM proveedores WHERE LOWER(nombre) = ? AND id <> ?", (nombre.lower(), pid)).fetchone():
        raise ValueError("Ya existe otro proveedor con ese nombre.")
    db.execute("UPDATE proveedores SET nombre = ?, telefono = ?, notas = ?, vigente = ? WHERE id = ?",
               (nombre, str(data.get("telefono", fila["telefono"] or ""))[:40], str(data.get("notas", fila["notas"] or ""))[:300],
                1 if data.get("vigente", bool(fila["vigente"])) else 0, pid))
    return jsonify({"success": True})


@app.route("/api/admin/compras", methods=["GET", "POST"])
@requiere_login(roles=("superusuario",))
@admin_api
def api_admin_compras(db):
    if request.method == "GET":
        solo_deuda = request.args.get("estado", "pendientes") != "todas"
        sql = ("SELECT c.*, p.nombre AS proveedor FROM compras c LEFT JOIN proveedores p ON p.id = c.proveedor_id "
               "WHERE c.anulada = 0")
        if solo_deuda:
            sql += " AND c.total_usd - c.pagado_usd > 0.004"
        filas = [dict(r) for r in db.execute(sql + " ORDER BY c.fecha DESC, c.id DESC LIMIT 300").fetchall()]
        pagos = {}
        if filas:
            ids = [f["id"] for f in filas]
            marcas = ",".join("?" for _ in ids)
            for r in db.execute(f"SELECT compra_id, monto_usd, metodo, nota, fecha FROM pagos_compra WHERE compra_id IN ({marcas}) ORDER BY id", tuple(ids)).fetchall():
                pagos.setdefault(r["compra_id"], []).append({"monto_usd": float(r["monto_usd"]), "metodo": r["metodo"] or "", "nota": r["nota"] or "", "fecha": str(r["fecha"])[:10]})
        return jsonify({"success": True, "compras": [{
            "id": f["id"], "proveedor_id": f["proveedor_id"], "proveedor": f["proveedor"] or "—", "sede_id": f["sede_id"],
            "descripcion": f["descripcion"] or "", "total_usd": float(f["total_usd"]), "pagado_usd": float(f["pagado_usd"]),
            "saldo_usd": round(float(f["total_usd"]) - float(f["pagado_usd"]), 2), "estado": f["estado"],
            "fecha": str(f["fecha"])[:10], "pagos": pagos.get(f["id"], [])} for f in filas]})

    data = _json_body()
    prov = _entero(data.get("proveedor_id"), "El proveedor")
    if not db.execute("SELECT id FROM proveedores WHERE id = ?", (prov,)).fetchone():
        raise ValueError("Proveedor no encontrado.")
    total = _num(data.get("total_usd"), "El total de la compra", 0.01)
    pagado = _num(data.get("pagado_usd") or 0, "El monto pagado", 0)
    if pagado > total + 0.004:
        raise ValueError("Lo pagado no puede ser mayor que el total de la compra.")
    sede = _entero(data.get("sede_id"), "La sede", False)
    fecha = _fecha(data.get("fecha"))
    desc = str(data.get("descripcion", "")).strip()[:200]
    cid = db.insert_returning_id(
        "INSERT INTO compras (proveedor_id, sede_id, descripcion, total_usd, pagado_usd, estado, fecha, usuario_id, anulada, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
        (prov, sede, desc, total, pagado, _estado_compra(total, pagado), fecha, g.user["id"], ahora_utc_str()))
    if pagado > 0.004:
        db.execute("INSERT INTO pagos_compra (compra_id, monto_usd, metodo, nota, fecha, usuario_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (cid, pagado, str(data.get("metodo", ""))[:40], "Pago inicial", fecha, g.user["id"], ahora_utc_str()))
    log_auditoria(db, "COMPRA_PROVEEDOR", "compras", cid, None, {"proveedor_id": prov, "total_usd": total, "pagado_usd": pagado}, desc or "Compra a proveedor", sede)
    return jsonify({"success": True, "id": cid})


@app.route("/api/admin/compras/<int:cid>/pago", methods=["POST"])
@requiere_login(roles=("superusuario",))
@admin_api
def api_admin_compra_pago(db, cid):
    data = _json_body()
    c = db.execute("SELECT * FROM compras WHERE id = ? AND anulada = 0", (cid,)).fetchone()
    if not c:
        raise ValueError("Compra no encontrada.")
    saldo = float(c["total_usd"]) - float(c["pagado_usd"])
    monto = _num(data.get("monto_usd"), "El monto del abono", 0.01)
    if monto > saldo + 0.004:
        raise ValueError(f"El abono ({monto:.2f}) es mayor que el saldo pendiente ({saldo:.2f}).")
    nuevo = float(c["pagado_usd"]) + monto
    fecha = _fecha(data.get("fecha"))
    db.execute("INSERT INTO pagos_compra (compra_id, monto_usd, metodo, nota, fecha, usuario_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
               (cid, monto, str(data.get("metodo", ""))[:40], str(data.get("nota", ""))[:200], fecha, g.user["id"], ahora_utc_str()))
    db.execute("UPDATE compras SET pagado_usd = ?, estado = ? WHERE id = ?", (nuevo, _estado_compra(float(c["total_usd"]), nuevo), cid))
    log_auditoria(db, "PAGO_PROVEEDOR", "compras", cid, {"pagado_usd": float(c["pagado_usd"])}, {"pagado_usd": nuevo}, f"Abono de {monto:.2f} USD")
    return jsonify({"success": True, "saldo_usd": round(float(c["total_usd"]) - nuevo, 2)})


@app.route("/api/admin/compras/<int:cid>/anular", methods=["POST"])
@requiere_login(roles=("superusuario",))
@admin_api
def api_admin_compra_anular(db, cid):
    data = _json_body()
    motivo = str(data.get("motivo", "")).strip()
    if not motivo:
        raise ValueError("Escribe el motivo de la anulación.")
    c = db.execute("SELECT * FROM compras WHERE id = ? AND anulada = 0", (cid,)).fetchone()
    if not c:
        raise ValueError("Compra no encontrada.")
    if float(c["pagado_usd"]) > 0.004:
        raise ValueError("La compra ya tiene pagos registrados y no se puede anular.")
    db.execute("UPDATE compras SET anulada = 1 WHERE id = ?", (cid,))
    log_auditoria(db, "COMPRA_PROVEEDOR", "compras", cid, {"anulada": False}, {"anulada": True}, motivo)
    return jsonify({"success": True})


@app.route("/api/admin/gastos", methods=["GET", "POST"])
@requiere_login(roles=("superusuario",))
@admin_api
def api_admin_gastos(db):
    if request.method == "GET":
        hoy = hoy_local()
        desde = _fecha(request.args.get("desde"), (hoy - datetime.timedelta(days=29)).isoformat())
        hasta = _fecha(request.args.get("hasta"), hoy.isoformat())
        sede = _entero(request.args.get("sede_id"), "La sede", False)
        sql = "SELECT * FROM gastos WHERE anulado = 0 AND fecha >= ? AND fecha <= ?"
        par = [desde, hasta]
        if sede:
            sql += " AND sede_id = ?"
            par.append(sede)
        filas = db.execute(sql + " ORDER BY fecha DESC, id DESC LIMIT 500", tuple(par)).fetchall()
        cats, propias = _categorias_gasto(db)
        return jsonify({"success": True, "categorias": cats, "categorias_propias": propias, "metodos": METODOS_PAGO,
                        "total_usd": round(sum(float(r["monto_usd"]) for r in filas), 2),
                        "gastos": [{"id": r["id"], "sede_id": r["sede_id"], "categoria": r["categoria"], "descripcion": r["descripcion"] or "",
                                    "monto_usd": float(r["monto_usd"]), "metodo": r["metodo"] or "", "fecha": str(r["fecha"])[:10]} for r in filas]})

    data = _json_body()
    categoria = str(data.get("categoria", "")).strip()[:60]
    if not categoria:
        raise ValueError("Elige una categoría para el gasto.")
    categoria = _registrar_categoria(db, categoria)  # si es nueva, queda guardada para la próxima
    monto = _num(data.get("monto_usd"), "El monto", 0.01)
    sede = _entero(data.get("sede_id"), "La sede", False)
    fecha = _fecha(data.get("fecha"))
    desc = str(data.get("descripcion", "")).strip()[:200]
    gid = db.insert_returning_id(
        "INSERT INTO gastos (sede_id, categoria, descripcion, monto_usd, metodo, proveedor_id, fecha, usuario_id, anulado, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
        (sede, categoria, desc, monto, str(data.get("metodo", ""))[:40], _entero(data.get("proveedor_id"), "El proveedor", False),
         fecha, g.user["id"], ahora_utc_str()))
    log_auditoria(db, "GASTO", "gastos", gid, None, {"categoria": categoria, "monto_usd": monto, "fecha": fecha}, desc or categoria, sede)
    return jsonify({"success": True, "id": gid})


@app.route("/api/admin/categorias-gasto", methods=["POST"])
@requiere_login(roles=("superusuario",))
@admin_api
def api_admin_categoria_crear(db):
    nombre = _registrar_categoria(db, _json_body().get("nombre"))
    return jsonify({"success": True, "nombre": nombre})


@app.route("/api/admin/categorias-gasto/quitar", methods=["POST"])
@requiere_login(roles=("superusuario",))
@admin_api
def api_admin_categoria_quitar(db):
    nombre = " ".join(str(_json_body().get("nombre", "")).split())
    if nombre.lower() in {c.lower() for c in CATEGORIAS_GASTO}:
        raise ValueError("Esa categoría viene de fábrica y no se puede quitar.")
    db.execute("UPDATE categorias_gasto SET activa = ? WHERE LOWER(nombre) = ?", (0, nombre.lower()))
    return jsonify({"success": True})


@app.route("/api/admin/gastos/<int:gid>/anular", methods=["POST"])
@requiere_login(roles=("superusuario",))
@admin_api
def api_admin_gasto_anular(db, gid):
    motivo = str(_json_body().get("motivo", "")).strip()
    if not motivo:
        raise ValueError("Escribe el motivo de la anulación.")
    gasto = db.execute("SELECT * FROM gastos WHERE id = ? AND anulado = 0", (gid,)).fetchone()
    if not gasto:
        raise ValueError("Gasto no encontrado.")
    db.execute("UPDATE gastos SET anulado = 1, anulado_motivo = ? WHERE id = ?", (motivo[:200], gid))
    log_auditoria(db, "GASTO", "gastos", gid, {"monto_usd": float(gasto["monto_usd"]), "categoria": gasto["categoria"]}, {"anulado": True}, motivo, gasto["sede_id"])
    return jsonify({"success": True})


def _limites_utc(desde, hasta):
    d0 = datetime.datetime.combine(datetime.date.fromisoformat(desde), datetime.time.min, tzinfo=TZ_LOCAL)
    d1 = datetime.datetime.combine(datetime.date.fromisoformat(hasta) + datetime.timedelta(days=1), datetime.time.min, tzinfo=TZ_LOCAL)
    f = "%Y-%m-%d %H:%M:%S"
    return d0.astimezone(datetime.timezone.utc).strftime(f), d1.astimezone(datetime.timezone.utc).strftime(f)


@app.route("/api/admin/finanzas/resumen", methods=["GET"])
@requiere_login(roles=("superusuario",))
@admin_api
def api_admin_finanzas_resumen(db):
    hoy = hoy_local()
    desde = _fecha(request.args.get("desde"), (hoy - datetime.timedelta(days=29)).isoformat())
    hasta = _fecha(request.args.get("hasta"), hoy.isoformat())
    if desde > hasta:
        raise ValueError("La fecha inicial es posterior a la final.")
    d_ini, d_fin = datetime.date.fromisoformat(desde), datetime.date.fromisoformat(hasta)
    if (d_fin - d_ini).days > 366:
        raise ValueError("El rango máximo es de un año.")
    sede = _entero(request.args.get("sede_id"), "La sede", False)
    u0, u1 = _limites_utc(desde, hasta)
    fs = " AND sede_id = ?" if sede else ""
    ps = (sede,) if sede else ()

    ventas = db.execute("SELECT sede_id, total_venta_usd, costo_insumos_usd, estado_pago, created_at, cobrado_at FROM ordenes "
                        "WHERE created_at >= ? AND created_at < ?" + fs, (u0, u1) + ps).fetchall()
    cobros = db.execute("SELECT total_venta_usd, cobrado_at FROM ordenes WHERE cobrado_at IS NOT NULL AND cobrado_at >= ? AND cobrado_at < ?" + fs,
                        (u0, u1) + ps).fetchall()
    pendientes = db.execute("SELECT cliente_nombre, total_venta_usd FROM ordenes WHERE estado_pago = 'FIADO'" + fs, ps).fetchall()
    gastos = db.execute("SELECT categoria, monto_usd, fecha FROM gastos WHERE anulado = 0 AND fecha >= ? AND fecha <= ?" + fs,
                        (desde, hasta) + ps).fetchall()
    fc = " AND c.sede_id = ?" if sede else ""
    pagos = db.execute("SELECT pc.monto_usd, pc.fecha FROM pagos_compra pc JOIN compras c ON c.id = pc.compra_id "
                       "WHERE c.anulada = 0 AND pc.fecha >= ? AND pc.fecha <= ?" + fc, (desde, hasta) + ps).fetchall()
    deudas = db.execute("SELECT c.total_usd, c.pagado_usd, pr.nombre AS proveedor FROM compras c LEFT JOIN proveedores pr ON pr.id = c.proveedor_id "
                        "WHERE c.anulada = 0 AND c.total_usd - c.pagado_usd > 0.004" + fc, ps).fetchall()

    dias, d = {}, d_ini
    while d <= d_fin:
        dias[d.isoformat()] = {"fecha": d.isoformat(), "ingresos": 0.0, "egresos": 0.0}
        d += datetime.timedelta(days=1)

    ventas_total = costo_total = ingreso_ventas = ingreso_cobros = 0.0
    for o in ventas:
        if o["estado_pago"] == "ANULADA":
            continue
        t = float(o["total_venta_usd"])
        ventas_total += t
        costo_total += float(o["costo_insumos_usd"])
        if o["estado_pago"] == "PAGADA" and not o["cobrado_at"]:
            loc = a_local(o["created_at"])
            if loc and loc.date().isoformat() in dias:
                dias[loc.date().isoformat()]["ingresos"] += t
                ingreso_ventas += t
    for c in cobros:
        loc, t = a_local(c["cobrado_at"]), float(c["total_venta_usd"])
        if loc and loc.date().isoformat() in dias:
            dias[loc.date().isoformat()]["ingresos"] += t
            ingreso_cobros += t

    por_categoria, egreso_gastos, egreso_prov = {}, 0.0, 0.0
    for r in gastos:
        m = float(r["monto_usd"])
        egreso_gastos += m
        por_categoria[r["categoria"]] = por_categoria.get(r["categoria"], 0.0) + m
        f = str(r["fecha"])[:10]
        if f in dias:
            dias[f]["egresos"] += m
    for r in pagos:
        m = float(r["monto_usd"])
        egreso_prov += m
        f = str(r["fecha"])[:10]
        if f in dias:
            dias[f]["egresos"] += m

    por_pagar, deuda_prov = 0.0, {}
    for r in deudas:
        s = float(r["total_usd"]) - float(r["pagado_usd"])
        por_pagar += s
        k = r["proveedor"] or "—"
        deuda_prov[k] = deuda_prov.get(k, 0.0) + s
    por_cobrar, fiado_cli = 0.0, {}
    for r in pendientes:
        t = float(r["total_venta_usd"])
        por_cobrar += t
        k = r["cliente_nombre"] or "Consumidor"
        fiado_cli[k] = fiado_cli.get(k, 0.0) + t

    ingresos = ingreso_ventas + ingreso_cobros
    egresos = egreso_gastos + egreso_prov
    ordenar = lambda dic: [{"nombre": k, "monto_usd": round(v, 2)} for k, v in sorted(dic.items(), key=lambda kv: -kv[1])]
    return jsonify({
        "success": True, "desde": desde, "hasta": hasta,
        "ventas_total_usd": round(ventas_total, 2), "costo_insumos_usd": round(costo_total, 2),
        "utilidad_bruta_usd": round(ventas_total - costo_total, 2),
        "ingresos_ventas_usd": round(ingreso_ventas, 2), "ingresos_cobros_fiado_usd": round(ingreso_cobros, 2),
        "ingresos_usd": round(ingresos, 2),
        "gastos_usd": round(egreso_gastos, 2), "pagos_proveedores_usd": round(egreso_prov, 2), "egresos_usd": round(egresos, 2),
        "flujo_neto_usd": round(ingresos - egresos, 2),
        "utilidad_operativa_usd": round(ventas_total - costo_total - egreso_gastos, 2),
        "por_cobrar_usd": round(por_cobrar, 2), "por_pagar_usd": round(por_pagar, 2),
        "serie": [{"fecha": v["fecha"], "ingresos": round(v["ingresos"], 2), "egresos": round(v["egresos"], 2)} for v in dias.values()],
        "gastos_por_categoria": ordenar(por_categoria), "deudas_por_proveedor": ordenar(deuda_prov), "fiados_por_cliente": ordenar(fiado_cli),
    })


# ----------------------------------------------------------------------
# FACTURA DIGITAL + ARCHIVOS EN GOOGLE DRIVE
# ----------------------------------------------------------------------
def _error_acceso_entidad(db, entidad, eid):
    """None si el usuario puede usar ese registro; si no, la respuesta de error lista para devolver."""
    tabla = ENTIDADES_ARCHIVO.get(entidad)
    if not tabla:
        return jsonify({"success": False, "error": "Tipo de archivo no válido."}), 400
    fila = db.execute(f"SELECT id FROM {tabla} WHERE id = ?", (eid,)).fetchone()  # tabla viene de una lista fija
    if not fila:
        return jsonify({"success": False, "error": "El registro no existe."}), 404
    if g.user["rol"] == "superusuario":
        return None
    if entidad != "orden":
        return jsonify({"success": False, "error": "No tienes permiso para esta acción."}), 403
    orden = db.execute("SELECT sede_id FROM ordenes WHERE id = ?", (eid,)).fetchone()
    if orden["sede_id"] != g.user["sede_id"]:
        return jsonify({"success": False, "error": "Esa orden es de otra sede."}), 403
    return None


@app.route("/factura/<token>", methods=["GET"])
def factura_publica(token):
    """La abre el cliente desde WhatsApp. No pide login: el enlace va firmado y no se puede adivinar."""
    try:
        oid = int(_firmador_factura.loads(token)["o"])
    except (BadSignature, KeyError, TypeError, ValueError):
        abort(404)
    db = get_db()
    try:
        datos = _datos_factura(db, oid)
    finally:
        db.close()
    if not datos:
        abort(404)
    resp = app.response_class(render_template_string(PLANTILLA_FACTURA, **datos), status=200, mimetype="text/html")
    resp.headers["Cache-Control"] = "private, no-store"
    resp.headers["X-Robots-Tag"] = "noindex"
    return resp


@app.route("/api/orden/<int:oid>/factura-link", methods=["GET"])
@requiere_login()
@admin_api
def api_orden_factura_link(db, oid):
    err = _error_acceso_entidad(db, "orden", oid)
    if err:
        return err
    datos = _datos_factura(db, oid)
    url = request.url_root.rstrip("/") + "/factura/" + _firmador_factura.dumps({"o": oid})
    nombre = (datos["cliente"] or "").strip()
    texto = (f"Hola {nombre}" if nombre else "Hola") + f", gracias por tu compra en {NEGOCIO_NOMBRE}. 🍔\n"
    texto += f"Tu factura {datos['numero']} por {datos['total']}"
    texto += " (pendiente de pago)" if datos["fiado"] else ""
    texto += f" está aquí:\n{url}"
    tel = normalizar_telefono_ve(datos["_telefono"])
    wa = (f"https://wa.me/{tel}" if tel else "https://wa.me/") + "?text=" + urllib.parse.quote(texto)
    return jsonify({"success": True, "url": url, "whatsapp_url": wa, "numero": datos["numero"], "tiene_telefono": bool(tel)})


@app.route("/api/archivos", methods=["POST"])
@requiere_login()
@admin_api
def api_archivos_subir(db):
    if not drive_configurado():
        return jsonify({"success": False, "error": "Google Drive no está configurado en el servidor (faltan DRIVE_SCRIPT_URL y DRIVE_SCRIPT_TOKEN)."}), 503
    entidad = request.form.get("entidad", "")
    eid = _entero(request.form.get("entidad_id"), "El registro")
    err = _error_acceso_entidad(db, entidad, eid)
    if err:
        return err
    f = request.files.get("file")
    data = f.read() if f else b""
    if not data:
        raise ValueError("No llegó ningún archivo.")
    n = db.execute("SELECT COUNT(*) AS c FROM archivos WHERE entidad = ? AND entidad_id = ?", (entidad, eid)).fetchone()
    if (n["c"] if isinstance(n, dict) else n[0]) >= 10:
        raise ValueError("Ya hay 10 archivos en este registro.")
    mime = (f.mimetype or "").lower()
    if mime.startswith("image/"):
        try:
            data, mime = comprimir_imagen(data)
        except Exception:
            raise ValueError("No se pudo leer la imagen. Prueba con una foto JPG o PNG.")
        ext = "jpg"
    elif mime == "application/pdf":
        ext = "pdf"
    else:
        return jsonify({"success": False, "error": "Solo se aceptan fotos o PDF."}), 415
    nombre = f"{entidad}_{eid}_{ahora_utc_str().replace(' ', '_').replace(':', '')}.{ext}"
    try:
        drive_id = drive_subir(nombre, data, mime)
    except Exception as e:
        print(f">> [DRIVE] Error subiendo {nombre}: {e}")
        return jsonify({"success": False, "error": "No se pudo subir a Google Drive. Revisa la conexión del servidor con Drive."}), 502
    aid = db.insert_returning_id(
        "INSERT INTO archivos (entidad, entidad_id, drive_file_id, nombre, mime, tamano, usuario_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (entidad, eid, drive_id, nombre, mime, len(data), g.user["id"], ahora_utc_str()))
    return jsonify({"success": True, "id": aid, "url": f"/api/archivos/{aid}"})


@app.route("/api/archivos/lista/<entidad>/<int:eid>", methods=["GET"])
@requiere_login()
@admin_api
def api_archivos_lista(db, entidad, eid):
    err = _error_acceso_entidad(db, entidad, eid)
    if err:
        return err
    filas = db.execute("SELECT id, nombre, mime FROM archivos WHERE entidad = ? AND entidad_id = ? ORDER BY id", (entidad, eid)).fetchall()
    return jsonify({"success": True, "archivos": [{"id": r["id"], "nombre": r["nombre"], "mime": r["mime"]} for r in filas]})


@app.route("/api/archivos/<int:aid>", methods=["GET"])
@requiere_login()
@admin_api
def api_archivos_ver(db, aid):
    """Permite usar <img src="/api/archivos/12">: el servidor trae la imagen desde Drive."""
    a = db.execute("SELECT entidad, entidad_id, drive_file_id, mime FROM archivos WHERE id = ?", (aid,)).fetchone()
    if not a:
        return jsonify({"success": False, "error": "Archivo no encontrado."}), 404
    err = _error_acceso_entidad(db, a["entidad"], a["entidad_id"])
    if err:
        return err
    try:
        data = drive_descargar(a["drive_file_id"])
    except Exception as e:
        print(f">> [DRIVE] Error leyendo archivo {aid}: {e}")
        return jsonify({"success": False, "error": "No se pudo leer el archivo desde Google Drive."}), 502
    resp = app.response_class(data, status=200, mimetype=a["mime"] or "application/octet-stream")
    resp.headers["Cache-Control"] = "private, max-age=86400"
    return resp


# ----------------------------------------------------------------------
# PEDIDOS WEB: menú público, mercancía apartada con vencimiento y verificación de pagos
# ----------------------------------------------------------------------
try:
    PEDIDO_VENCE_MIN = max(3, int(os.environ.get("PEDIDO_VENCE_MIN", "15")))
except ValueError:
    PEDIDO_VENCE_MIN = 15
_firmador_pedido = URLSafeSerializer(app.secret_key, salt="pedido-publico")
_LOCK_RESERVA = threading.Lock()
_RATE_PEDIDOS = {}
METODOS_PAGO_WEB = ("Pago móvil", "Transferencia", "Zelle", "Otro")
FMT_UTC = "%Y-%m-%d %H:%M:%S"


def _cfg_tasa(db):
    cfg = db.execute("SELECT * FROM configuracion WHERE id = 1").fetchone()
    return float(cfg["tasa_manual"] if cfg["usar_tasa_manual"] else cfg["tasa_bcv"]), float(cfg["tasa_reposicion"])


def _usuario_web_id(db):
    r = db.execute("SELECT id FROM usuarios WHERE username = 'pedido_web'").fetchone()
    if not r:
        r = db.execute("SELECT id FROM usuarios WHERE rol = 'superusuario' ORDER BY id LIMIT 1").fetchone()
    return r["id"]


def _sedes_publicas(db):
    return [dict(x) for x in db.execute("SELECT id, nombre FROM sedes WHERE activa = 1 AND es_almacen = 0 ORDER BY id").fetchall()]


def _disponibilidad(db, sede_id):
    """producto_id -> unidades que se pueden vender ahora. None = el producto no tiene receta (no se controla)."""
    stock = {r["insumo_id"]: float(r["stock_actual"]) for r in
             db.execute("SELECT insumo_id, stock_actual FROM inventario_por_sede WHERE sede_id = ?", (sede_id,)).fetchall()}
    recetas = {}
    for r in db.execute("SELECT producto_id, insumo_id, cantidad_consumida FROM recetas_bom").fetchall():
        if float(r["cantidad_consumida"]) > 0:
            recetas.setdefault(r["producto_id"], []).append((r["insumo_id"], float(r["cantidad_consumida"])))
    return {pid: max(0, int(min(stock.get(iid, 0.0) / c for iid, c in filas) + 1e-9)) for pid, filas in recetas.items()}


def _cerrar_pedido_web(db, oid, nuevo_estado, motivo, usuario_id):
    """Libera la mercancía apartada. Idempotente: solo actúa si el pedido sigue activo (no repone dos veces)."""
    cur = db.execute("UPDATE ordenes SET estado_web = ?, vencimiento = NULL WHERE id = ? AND estado_web IN ('PENDIENTE_PAGO', 'PAGO_REPORTADO')",
                     (nuevo_estado, oid))
    if cur.rowcount < 1:
        return False
    o = db.execute("SELECT sede_id FROM ordenes WHERE id = ?", (oid,)).fetchone()
    ahora = ahora_utc_str()
    filas = db.execute("SELECT insumo_id, cantidad FROM movimientos_kardex WHERE tipo = 'VENTA' AND motivo = ?",
                       (f"Reserva pedido web #{oid}",)).fetchall()
    for f in filas:
        devolver = -float(f["cantidad"])
        ajustar_stock(db, o["sede_id"], f["insumo_id"], devolver, permitir_negativo=True)
        db.execute("INSERT INTO movimientos_kardex (sede_id, insumo_id, usuario_id, tipo, cantidad, costo_unitario_momento, motivo, created_at) "
                   "VALUES (?, ?, ?, 'REVERSION_AUDITORIA', ?, 0, ?, ?)",
                   (o["sede_id"], f["insumo_id"], usuario_id, devolver, f"Pedido web #{oid}: {motivo}", ahora))
    return True


def liberar_vencidos(db):
    """Pedidos que no pagaron a tiempo: la mercancía vuelve al inventario."""
    ids = [r["id"] for r in db.execute("SELECT id FROM ordenes WHERE estado_web = 'PENDIENTE_PAGO' AND vencimiento < ?", (ahora_utc_str(),)).fetchall()]
    liberados = 0
    for oid in ids:
        if _cerrar_pedido_web(db, oid, "EXPIRADA", "venció el tiempo para pagar", _usuario_web_id(db)):
            liberados += 1
    if ids:
        db.commit()
    return liberados


def _segundos_restantes(venc):
    if not venc:
        return 0
    try:
        v = datetime.datetime.strptime(str(venc)[:19], FMT_UTC).replace(tzinfo=datetime.timezone.utc)
        return max(0, int((v - datetime.datetime.now(datetime.timezone.utc)).total_seconds()))
    except ValueError:
        return 0


def _ip_cliente():
    return (request.headers.get("X-Forwarded-For", request.remote_addr or "") or "").split(",")[0].strip() or "?"


def _marcas_ip(ip, clave, ventana=3600):
    ahora = time.time()
    marcas = [x for x in _RATE_PEDIDOS.get((ip, clave), []) if ahora - x < ventana]
    _RATE_PEDIDOS[(ip, clave)] = marcas
    return marcas


def _limite_ip_superado(ip, clave, maximo):
    with _LOCK_INTENTOS:
        return len(_marcas_ip(ip, clave)) >= maximo


def _anotar_ip(ip, clave):
    with _LOCK_INTENTOS:
        _marcas_ip(ip, clave).append(time.time())


def _oid_desde_token(token):
    try:
        return int(_firmador_pedido.loads(token)["p"])
    except (BadSignature, KeyError, TypeError, ValueError):
        raise ValueError("El enlace de tu pedido no es válido.")


def _estado_publico(db, oid):
    o = db.execute("SELECT o.*, s.nombre AS sede_nombre FROM ordenes o LEFT JOIN sedes s ON s.id = o.sede_id "
                   "WHERE o.id = ? AND o.origen = 'WEB'", (oid,)).fetchone()
    if not o:
        return None
    o = dict(o)
    tasa = float(o["tasa_aplicada"] or 0)
    filas = db.execute("SELECT i.cantidad, i.precio_unitario_usd, p.nombre FROM orden_items i "
                       "LEFT JOIN catalogo_productos p ON p.id = i.producto_id WHERE i.orden_id = ? ORDER BY i.id", (oid,)).fetchall()
    items = [{"nombre": r["nombre"] or "Producto", "cantidad": int(r["cantidad"]),
              "total_bs": round(round(float(r["precio_unitario_usd"]) * tasa, 2) * int(r["cantidad"]), 2)} for r in filas]
    estado = o.get("estado_web") or ""
    res = {"id": oid, "estado": estado, "sede": o.get("sede_nombre") or "", "cliente": o.get("cliente_nombre") or "",
           "items": items, "total_bs": round(float(o["total_venta_usd"]) * tasa, 2),
           "segundos_restantes": _segundos_restantes(o.get("vencimiento")) if estado == "PENDIENTE_PAGO" else 0,
           "vence_min": PEDIDO_VENCE_MIN, "referencia": o.get("referencia_pago") or "", "pago_metodo": o.get("pago_metodo") or "",
           "contacto": NEGOCIO_TEL or ""}
    if estado == "PENDIENTE_PAGO":
        qr = db.execute("SELECT titulo, banco, telefono, rif_cedula, qr_base64 FROM metodos_pago_qr WHERE activo = 1 ORDER BY id LIMIT 1").fetchone()
        res["pago"] = dict(qr) if qr else None
    if estado == "CONFIRMADA":
        res["factura_url"] = request.url_root.rstrip("/") + "/factura/" + _firmador_factura.dumps({"o": oid})
    return res


@app.route("/api/publico/menu", methods=["GET"])
@admin_api
def api_publico_menu(db):
    """Lo que ve cualquier persona en el link principal: productos, precios y cuánto hay. Nada más."""
    liberar_vencidos(db)
    sedes = _sedes_publicas(db)
    if not sedes:
        return jsonify({"success": True, "negocio": NEGOCIO_NOMBRE, "vence_min": PEDIDO_VENCE_MIN, "sedes": [], "sede_id": None, "productos": []})
    sid = request.args.get("sede_id", type=int)
    if sid not in [x["id"] for x in sedes]:
        sid = sedes[0]["id"]
    tasa, _ = _cfg_tasa(db)
    disp = _disponibilidad(db, sid)
    productos = []
    for p in db.execute("SELECT id, nombre, tipo, precio_venta_usd FROM catalogo_productos WHERE activo = 1 ORDER BY id").fetchall():
        d = disp.get(p["id"])
        usd = float(p["precio_venta_usd"])
        productos.append({"id": p["id"], "nombre": p["nombre"], "tipo": p["tipo"], "precio_usd": round(usd, 2),
                          "precio_bs": round(usd * tasa, 2), "disponible": None if d is None else min(d, 99)})
    return jsonify({"success": True, "negocio": NEGOCIO_NOMBRE, "vence_min": PEDIDO_VENCE_MIN,
                    "sedes": sedes, "sede_id": sid, "productos": productos})


@app.route("/api/publico/pedido", methods=["POST"])
@admin_api
def api_publico_pedido_crear(db):
    data = _json_body()
    if str(data.get("web_check", "")).strip():
        raise ValueError("Solicitud no válida.")
    ip = _ip_cliente()
    _anotar_ip(ip, "intentos")
    if _limite_ip_superado(ip, "intentos", 60) or _limite_ip_superado(ip, "creados", 6):
        return jsonify({"success": False, "error": "Hiciste demasiados pedidos seguidos. Intenta de nuevo en un rato."}), 429
    nombre = str(data.get("nombre", "")).strip()[:60]
    if len(nombre) < 2:
        raise ValueError("Escribe tu nombre.")
    tel = normalizar_telefono_ve(str(data.get("telefono", "")))
    if not (11 <= len(tel) <= 15):
        raise ValueError("Escribe un teléfono válido, por ejemplo 0412-1234567.")
    notas = str(data.get("notas", "")).strip()[:200]
    sede_id = _entero(data.get("sede_id"), "La sede")
    if sede_id not in [x["id"] for x in _sedes_publicas(db)]:
        raise ValueError("Esa sede no está disponible.")
    items_in = data.get("items")
    if not isinstance(items_in, list) or not items_in or len(items_in) > 15:
        raise ValueError("Elige al menos un producto.")

    liberar_vencidos(db)
    pendientes = db.execute("SELECT COUNT(*) AS c FROM ordenes WHERE estado_web = 'PENDIENTE_PAGO' AND cliente_telefono = ?", (tel,)).fetchone()
    if int(pendientes["c"] if isinstance(pendientes, dict) else pendientes[0]) >= 2:
        raise ValueError("Ya tienes pedidos esperando pago. Págalos o espera a que venzan para pedir de nuevo.")

    with _LOCK_RESERVA:  # evita que dos clientes aparten la última unidad al mismo tiempo
        tasa, tasa_rep = _cfg_tasa(db)
        stock = {r["insumo_id"]: float(r["stock_actual"]) for r in
                 db.execute("SELECT insumo_id, stock_actual FROM inventario_por_sede WHERE sede_id = ?", (sede_id,)).fetchall()}
        costos = {r["id"]: float(r["costo_unitario_usd"]) for r in db.execute("SELECT id, costo_unitario_usd FROM insumos_maestro").fetchall()}
        total_usd = costo_total = 0.0
        unidades = 0
        consumo, lineas = {}, []
        for it in items_in:
            pid = _entero(it.get("producto_id"), "El producto")
            cant = _entero(it.get("cantidad"), "La cantidad")
            if cant < 1 or cant > 20:
                raise ValueError("Cantidad no válida.")
            unidades += cant
            if unidades > 40:
                raise ValueError("Es un pedido muy grande. Para más de 40 unidades escríbenos directamente.")
            prod = db.execute("SELECT * FROM catalogo_productos WHERE id = ? AND activo = 1", (pid,)).fetchone()
            if not prod:
                raise ValueError("Uno de los productos ya no está disponible.")
            precio = float(prod["precio_venta_usd"])
            costo_unit = 0.0
            for b in db.execute("SELECT insumo_id, cantidad_consumida FROM recetas_bom WHERE producto_id = ?", (pid,)).fetchall():
                c = float(b["cantidad_consumida"])
                costo_unit += costos.get(b["insumo_id"], 0.0) * c
                consumo[b["insumo_id"]] = consumo.get(b["insumo_id"], 0.0) + c * cant
                if stock.get(b["insumo_id"], 0.0) + 1e-9 < consumo[b["insumo_id"]]:
                    raise ValueError(f"Ya no hay suficiente de «{prod['nombre']}». Ajusta la cantidad e intenta de nuevo.")
            total_usd += precio * cant
            costo_total += costo_unit * cant
            lineas.append((pid, cant, precio, costo_unit))

        ahora = ahora_utc_str()
        vence = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=PEDIDO_VENCE_MIN)).strftime(FMT_UTC)
        web_id = _usuario_web_id(db)
        oid = db.insert_returning_id("""
            INSERT INTO ordenes (
                sede_id, cajero_id, cliente_nombre, cliente_telefono,
                total_venta_usd, costo_insumos_usd, tasa_aplicada, tasa_reposicion_momento,
                recibido_usd, recibido_bs, vuelto_usd, vuelto_bs,
                estado_pago, estado_cocina, notas_cocina, referencia_pago, created_at,
                origen, estado_web, vencimiento, ip_creacion
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0, 'ANULADA', 'PREPARANDO', ?, '', ?, 'WEB', 'PENDIENTE_PAGO', ?, ?)
        """, (sede_id, web_id, nombre, tel, total_usd, costo_total, tasa, tasa_rep, notas, ahora, vence, _ip_cliente()))
        for pid, cant, precio, costo_unit in lineas:
            db.execute("INSERT INTO orden_items (orden_id, producto_id, cantidad, precio_unitario_usd, costo_unitario_usd, modificadores_json) "
                       "VALUES (?, ?, ?, ?, ?, '[]')", (oid, pid, cant, precio, costo_unit))
        for iid, cant in consumo.items():
            ajustar_stock(db, sede_id, iid, -cant)
            db.execute("INSERT INTO movimientos_kardex (sede_id, insumo_id, usuario_id, tipo, cantidad, costo_unitario_momento, motivo, created_at) "
                       "VALUES (?, ?, ?, 'VENTA', ?, ?, ?, ?)", (sede_id, iid, web_id, -cant, costos.get(iid, 0.0), f"Reserva pedido web #{oid}", ahora))
    _anotar_ip(ip, "creados")
    return jsonify({"success": True, "orden_id": oid, "token": _firmador_pedido.dumps({"p": oid})})


@app.route("/api/publico/pedido/<token>", methods=["GET"])
@admin_api
def api_publico_pedido_estado(db, token):
    oid = _oid_desde_token(token)
    liberar_vencidos(db)
    p = _estado_publico(db, oid)
    if not p:
        raise ValueError("Pedido no encontrado.")
    return jsonify({"success": True, "pedido": p})


@app.route("/api/publico/pedido/<token>/pago", methods=["POST"])
@admin_api
def api_publico_pedido_pago(db, token):
    oid = _oid_desde_token(token)
    liberar_vencidos(db)
    o = db.execute("SELECT * FROM ordenes WHERE id = ? AND origen = 'WEB'", (oid,)).fetchone()
    if not o:
        raise ValueError("Pedido no encontrado.")
    if o["estado_web"] == "PAGO_REPORTADO":
        raise ValueError("Ya registraste el pago de este pedido. Lo estamos verificando.")
    if o["estado_web"] != "PENDIENTE_PAGO":
        raise ValueError("Este pedido ya no está esperando pago (venció o fue cerrado). Haz un pedido nuevo.")
    metodo = request.form.get("metodo", "")
    if metodo not in METODOS_PAGO_WEB:
        raise ValueError("Elige cómo pagaste.")
    ref = re.sub(r"[^A-Za-z0-9\-]", "", request.form.get("referencia", "")).strip()[:30]
    try:
        monto = max(0.0, float(request.form.get("monto_bs") or 0))
    except ValueError:
        monto = 0.0
    f = request.files.get("file")
    data = f.read() if f else b""
    if len(ref) < 4 and not data:
        raise ValueError("Escribe el número de referencia del pago o adjunta la captura del comprobante.")
    if len(data) > 12 * 1024 * 1024:
        raise ValueError("La imagen es demasiado grande.")

    foto_guardada = False
    if data and drive_configurado():
        n = db.execute("SELECT COUNT(*) AS c FROM archivos WHERE entidad = 'orden' AND entidad_id = ?", (oid,)).fetchone()
        if int(n["c"] if isinstance(n, dict) else n[0]) < 3:
            try:
                img, mime = comprimir_imagen(data)
            except Exception:
                raise ValueError("No se pudo leer la imagen. Usa una foto JPG o PNG.")
            nombre = f"web_{oid}_{ahora_utc_str().replace(' ', '_').replace(':', '')}.jpg"
            try:
                drive_id = drive_subir(nombre, img, mime)
                db.execute("INSERT INTO archivos (entidad, entidad_id, drive_file_id, nombre, mime, tamano, usuario_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                           ("orden", oid, drive_id, nombre, mime, len(img), _usuario_web_id(db), ahora_utc_str()))
                foto_guardada = True
            except Exception as e:
                print(f">> [DRIVE] No se pudo guardar el comprobante del pedido web #{oid}: {e}")
    if data and not foto_guardada and len(ref) < 4:
        raise ValueError("No pudimos guardar tu foto ahora. Escribe el número de referencia para continuar.")

    cur = db.execute("UPDATE ordenes SET estado_web = 'PAGO_REPORTADO', referencia_pago = ?, pago_metodo = ?, pago_monto_bs = ?, "
                     "pago_reportado_at = ?, vencimiento = NULL WHERE id = ? AND estado_web = 'PENDIENTE_PAGO'",
                     (ref, metodo, monto, ahora_utc_str(), oid))
    if cur.rowcount < 1:
        raise ValueError("Este pedido ya no está esperando pago. Haz un pedido nuevo.")
    return jsonify({"success": True, "foto_guardada": foto_guardada})


def _pedido_web_del_usuario(db, oid):
    o = db.execute("SELECT * FROM ordenes WHERE id = ? AND origen = 'WEB'", (oid,)).fetchone()
    if not o:
        raise ValueError("Pedido no encontrado.")
    if g.user["rol"] != "superusuario" and o["sede_id"] != g.user["sede_id"]:
        raise ValueError("Ese pedido es de otra sede.")
    return dict(o)


@app.route("/api/pedidos-web", methods=["GET"])
@requiere_login()
@admin_api
def api_pedidos_web(db):
    liberar_vencidos(db)
    es_super = g.user["rol"] == "superusuario"
    hace12h = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=12)).strftime(FMT_UTC)
    filtro = "" if es_super else " AND o.sede_id = ?"
    ps = () if es_super else (g.user["sede_id"],)
    filas = [dict(r) for r in db.execute(
        "SELECT o.*, s.nombre AS sede_nombre FROM ordenes o LEFT JOIN sedes s ON s.id = o.sede_id WHERE o.origen = 'WEB' AND "
        "(o.estado_web IN ('PENDIENTE_PAGO', 'PAGO_REPORTADO') OR (o.estado_web IN ('EXPIRADA', 'RECHAZADA') AND o.created_at >= ?))"
        + filtro + " ORDER BY o.id DESC LIMIT 100", (hace12h,) + ps).fetchall()]
    ids = [f["id"] for f in filas]
    items, archivos = {}, {}
    if ids:
        marca = ",".join("?" for _ in ids)
        for r in db.execute(f"SELECT i.orden_id, i.cantidad, p.nombre FROM orden_items i LEFT JOIN catalogo_productos p ON p.id = i.producto_id "
                            f"WHERE i.orden_id IN ({marca}) ORDER BY i.id", tuple(ids)).fetchall():
            items.setdefault(r["orden_id"], []).append({"nombre": r["nombre"] or "Producto", "cantidad": int(r["cantidad"])})
        for r in db.execute(f"SELECT id, entidad_id FROM archivos WHERE entidad = 'orden' AND entidad_id IN ({marca}) ORDER BY id", tuple(ids)).fetchall():
            archivos.setdefault(r["entidad_id"], []).append(r["id"])
    pedidos = []
    for f in filas:
        tasa = float(f["tasa_aplicada"] or 0)
        dup = 0
        if f.get("referencia_pago") and f["estado_web"] == "PAGO_REPORTADO":
            d = db.execute("SELECT COUNT(*) AS c FROM ordenes WHERE referencia_pago = ? AND id <> ?", (f["referencia_pago"], f["id"])).fetchone()
            dup = int(d["c"] if isinstance(d, dict) else d[0])
        creado = a_local(f.get("created_at"))
        rep_at = a_local(f.get("pago_reportado_at"))
        pedidos.append({
            "id": f["id"], "estado": f["estado_web"], "sede_id": f["sede_id"], "sede_nombre": f.get("sede_nombre") or "",
            "cliente": f.get("cliente_nombre") or "", "telefono": f.get("cliente_telefono") or "", "notas": f.get("notas_cocina") or "",
            "total_bs": round(float(f["total_venta_usd"]) * tasa, 2), "total_usd": round(float(f["total_venta_usd"]), 2),
            "items": items.get(f["id"], []), "archivos": archivos.get(f["id"], []),
            "segundos_restantes": _segundos_restantes(f.get("vencimiento")) if f["estado_web"] == "PENDIENTE_PAGO" else 0,
            "pago_metodo": f.get("pago_metodo") or "", "referencia": f.get("referencia_pago") or "",
            "monto_reportado_bs": float(f["pago_monto_bs"] or 0), "referencia_repetida": dup > 0,
            "creado": creado.strftime("%H:%M") if creado else "", "reportado": rep_at.strftime("%H:%M") if rep_at else "",
        })
    return jsonify({"success": True, "pedidos": pedidos})


@app.route("/api/pedidos-web/<int:oid>/confirmar", methods=["POST"])
@requiere_login()
@admin_api
def api_pedido_web_confirmar(db, oid):
    liberar_vencidos(db)
    o = _pedido_web_del_usuario(db, oid)
    if o["estado_web"] not in ("PENDIENTE_PAGO", "PAGO_REPORTADO"):
        raise ValueError("Este pedido ya no está pendiente (venció o ya fue atendido).")
    metodo = (o.get("pago_metodo") or str(_json_body().get("metodo") or "Pago móvil"))[:40]
    total_usd, tasa = float(o["total_venta_usd"]), float(o["tasa_aplicada"])
    rec_usd, rec_bs = (total_usd, 0.0) if metodo == "Zelle" else (0.0, round(total_usd * tasa, 2))
    cur = db.execute(
        "UPDATE ordenes SET estado_pago = 'PAGADA', estado_web = 'CONFIRMADA', estado_cocina = 'PREPARANDO', cobrado_at = ?, metodo_cobro = ?, "
        "recibido_usd = ?, recibido_bs = ?, vencimiento = NULL WHERE id = ? AND estado_web IN ('PENDIENTE_PAGO', 'PAGO_REPORTADO')",
        (ahora_utc_str(), metodo, rec_usd, rec_bs, oid))
    if cur.rowcount < 1:
        raise ValueError("Este pedido ya no está pendiente.")
    return jsonify({"success": True})


@app.route("/api/pedidos-web/<int:oid>/rechazar", methods=["POST"])
@requiere_login()
@admin_api
def api_pedido_web_rechazar(db, oid):
    _pedido_web_del_usuario(db, oid)
    motivo = str(_json_body().get("motivo", "")).strip()[:120] or "rechazado por el personal"
    if not _cerrar_pedido_web(db, oid, "RECHAZADA", motivo, g.user["id"]):
        raise ValueError("Este pedido ya no está pendiente.")
    return jsonify({"success": True})


def _hilo_vencimientos():
    """Cada minuto libera la mercancía de los pedidos que no pagaron a tiempo (además de hacerlo en cada consulta)."""
    while True:
        time.sleep(60)
        try:
            d = get_db()
            try:
                liberar_vencidos(d)
            finally:
                d.close()
        except Exception as e:
            print(f">> [WARN] Revisión de vencimientos: {e}")


threading.Thread(target=_hilo_vencimientos, daemon=True).start()


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

    // Los teclados en español solo traen la coma. Todos los campos numéricos aceptan coma o punto como decimal.
    const normalizarDecimal = (t) => {
      const limpio = String(t == null ? '' : t).replace(/[^0-9.,]/g, '');
      const i = Math.max(limpio.lastIndexOf(','), limpio.lastIndexOf('.'));
      if (i < 0) return limpio;
      return limpio.slice(0, i).replace(/[.,]/g, '') + '.' + limpio.slice(i + 1).replace(/[.,]/g, '');
    };
    // Número de WhatsApp en formato internacional (Venezuela: 0412-1234567 -> 584121234567)
    const telWhatsApp = (t) => {
      const d = String(t || '').replace(/\D/g, '');
      if (!d) return '';
      if (d.startsWith('58')) return d;
      if (d.startsWith('0')) return '58' + d.slice(1);
      if (d.length === 10) return '58' + d;
      return d;
    };
    function DecimalInput(props) {
      const { value, onChange, min, max, step, ...resto } = props;
      const controlado = value !== undefined;
      const aTexto = (v) => (v === undefined || v === null ? '' : String(v));
      const [texto, setTexto] = useState(aTexto(value));
      useEffect(() => {
        if (!controlado) return;
        const n = parseFloat(normalizarDecimal(texto));
        const v = (value === null || value === '') ? NaN : Number(value);
        if (Number.isNaN(n) && Number.isNaN(v)) return;
        if (texto === '' && v === 0) return;
        if (n !== v) setTexto(aTexto(value));
      }, [value]);
      const cambiar = (e) => {
        const bruto = e.target.value;
        const normal = normalizarDecimal(bruto);
        setTexto(controlado ? bruto.replace(/[^0-9.,]/g, '') : normal);
        if (onChange) {
          onChange({ target: { value: normal, name: resto.name, id: resto.id }, currentTarget: { value: normal }, persist: () => {}, preventDefault: () => {} });
        }
      };
      return <input {...resto} type="text" inputMode="decimal" autoComplete="off" value={texto} onChange={cambiar} />;
    }
    const _crearElementoOriginal = React.createElement;
    React.createElement = function (tipo, props, ...hijos) {
      if (tipo === 'input' && props && props.type === 'number' && props.step !== 1 && props.step !== '1') {
        return _crearElementoOriginal.call(React, DecimalInput, props, ...hijos);
      }
      return _crearElementoOriginal.call(React, tipo, props, ...hijos);
    };

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

// ==========================================================
// PANEL DE ADMINISTRACIÓN (solo superusuario)
// Finanzas, gastos, deudas con proveedores, catálogo y usuarios
// ==========================================================
const fmtUSD = (n) => (Number(n) || 0).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
// Fecha "de hoy" en hora de Venezuela (UTC-4), no en UTC: así las ventas de la noche cuentan para el día correcto.
const hoyCaracas = (offsetDias = 0) => new Date(Date.now() - 4 * 3600000 - offsetDias * 86400000).toISOString().split('T')[0];
const inicioMesCaracas = () => hoyCaracas().slice(0, 8) + '01';

async function apiAdmin(url, metodo = 'GET', cuerpo) {
  try {
    const res = await fetch(url, {
      method: metodo,
      headers: { 'Content-Type': 'application/json' },
      body: cuerpo !== undefined ? JSON.stringify(cuerpo) : undefined
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.success === false) return { success: false, error: data.error || ('Error ' + res.status) };
    return data;
  } catch (e) {
    return { success: false, error: 'Sin conexión con el servidor.' };
  }
}

const inputCls = 'w-full bg-[#09090b] border border-[#222227] rounded-xl p-2.5 text-white text-xs';
const btnPri = 'bg-[#2563eb] hover:bg-[#1d4ed8] disabled:opacity-50 text-white text-xs font-bold px-4 py-2.5 rounded-xl transition';
const btnSec = 'bg-[#18181b] hover:bg-[#222227] border border-[#222227] text-zinc-200 text-xs font-bold px-3 py-2 rounded-xl transition';
const btnPeligro = 'bg-red-950/60 hover:bg-red-900/60 border border-red-900 text-red-300 text-xs font-bold px-3 py-2 rounded-xl transition';
const cardCls = 'bg-[#111114] border border-[#222227] p-4 rounded-2xl space-y-3';

// ---------- Facturas y comprobantes (Google Drive) ----------
async function subirComprobante(entidad, entidadId, file) {
  try {
    const fd = new FormData();
    fd.append('file', file);
    fd.append('entidad', entidad);
    fd.append('entidad_id', entidadId);
    const res = await fetch('/api/archivos', { method: 'POST', body: fd });
    const d = await res.json().catch(() => ({}));
    if (!res.ok || d.success === false) return { success: false, error: d.error || ('Error ' + res.status) };
    return d;
  } catch (e) {
    return { success: false, error: 'Sin conexión con el servidor.' };
  }
}

// whatsapp = true abre WhatsApp con la factura lista; false muestra la factura en pantalla.
async function abrirFactura(ordenId, whatsapp) {
  // La pestaña se abre ANTES de esperar al servidor; si no, el navegador del celular la bloquea.
  const ventana = window.open('', '_blank');
  const r = await apiAdmin(`/api/orden/${ordenId}/factura-link`);
  if (!r.success) {
    if (ventana) ventana.close();
    alert(r.error);
    return;
  }
  const destino = whatsapp ? r.whatsapp_url : r.url;
  if (ventana) ventana.location.href = destino; else window.location.href = destino;
}

function Comprobantes({ entidad, entidadId, cantidad = 0, etiqueta = 'Comprobantes' }) {
  const [abierto, setAbierto] = useState(false);
  const [archivos, setArchivos] = useState(null);
  const [subiendo, setSubiendo] = useState(false);
  const [visor, setVisor] = useState(null);

  const cargar = async () => {
    const r = await apiAdmin(`/api/archivos/lista/${entidad}/${entidadId}`);
    if (r.success) setArchivos(r.archivos); else alert(r.error);
  };
  const alternar = () => {
    const nuevo = !abierto;
    setAbierto(nuevo);
    if (nuevo && archivos === null) cargar();
  };
  const elegir = async (e) => {
    const f = e.target.files && e.target.files[0];
    e.target.value = '';
    if (!f) return;
    setSubiendo(true);
    const r = await subirComprobante(entidad, entidadId, f);
    setSubiendo(false);
    if (!r.success) { alert(r.error); return; }
    cargar();
  };
  const total = archivos ? archivos.length : cantidad;

  return (
    <div className="space-y-2">
      <button type="button" onClick={alternar} className={btnSec}>📎 {etiqueta}{total > 0 ? ` (${total})` : ''}</button>
      {abierto && (
        <div className="bg-[#09090b] border border-[#222227] rounded-xl p-3 space-y-2">
          <div className="flex flex-wrap gap-2">
            {archivos === null && <span className="text-[11px] text-zinc-500">Cargando…</span>}
            {archivos && archivos.length === 0 && <span className="text-[11px] text-zinc-500">Aún no hay archivos.</span>}
            {(archivos || []).map(a => (
              a.mime === 'application/pdf' ? (
                <a key={a.id} href={`/api/archivos/${a.id}`} target="_blank" rel="noreferrer"
                  className="w-16 h-16 rounded-lg border border-[#222227] flex items-center justify-center text-[11px] text-[#60a5fa]">📄 PDF</a>
              ) : (
                <img key={a.id} src={`/api/archivos/${a.id}`} alt="Comprobante" onClick={() => setVisor(a)}
                  className="w-16 h-16 object-cover rounded-lg border border-[#222227] cursor-pointer" />
              )
            ))}
          </div>
          <label className={btnPri + ' inline-block cursor-pointer'}>
            {subiendo ? 'Subiendo a Drive…' : '+ Agregar foto o PDF'}
            <input type="file" accept="image/*,application/pdf" className="hidden" disabled={subiendo} onChange={elegir} />
          </label>
        </div>
      )}
      {visor && (
        <div onClick={() => setVisor(null)} className="fixed inset-0 z-[60] bg-black/90 flex items-center justify-center p-4">
          <img src={`/api/archivos/${visor.id}`} alt="Comprobante" className="max-w-full max-h-full object-contain rounded-xl" />
        </div>
      )}
    </div>
  );
}

function AccionesOrden({ ord }) {
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <button type="button" onClick={() => abrirFactura(ord.id, true)} className={btnSec}>📲 Factura por WhatsApp</button>
        <button type="button" onClick={() => abrirFactura(ord.id, false)} className={btnSec}>🧾 Ver factura</button>
      </div>
      <Comprobantes entidad="orden" entidadId={ord.id} cantidad={(ord.archivos || []).length} />
    </div>
  );
}

function Campo({ label, children }) {
  return (
    <div>
      <label className="text-zinc-400 text-[11px] block mb-1">{label}</label>
      {children}
    </div>
  );
}

function Kpi({ titulo, valor, tono = 'text-white', nota }) {
  return (
    <div className="bg-[#111114] border border-[#222227] rounded-2xl p-3">
      <div className="text-[10px] uppercase tracking-wider text-zinc-500 font-bold">{titulo}</div>
      <div className={`text-lg font-black font-mono mt-1 ${tono}`}>${fmtUSD(valor)}</div>
      {nota && <div className="text-[10px] text-zinc-500 mt-0.5">{nota}</div>}
    </div>
  );
}

function ListaBarras({ filas, color = 'bg-[#2563eb]', vacio }) {
  const max = Math.max(1, ...filas.map(f => f.monto_usd));
  if (!filas.length) return <div className="text-xs text-zinc-500 py-2">{vacio}</div>;
  return (
    <div className="space-y-2">
      {filas.map(f => (
        <div key={f.nombre}>
          <div className="flex justify-between text-xs">
            <span className="text-zinc-200 truncate pr-2">{f.nombre}</span>
            <span className="font-mono font-bold text-white">${fmtUSD(f.monto_usd)}</span>
          </div>
          <div className="h-1.5 bg-[#09090b] rounded-full mt-1 overflow-hidden">
            <div className={`h-full ${color} rounded-full`} style={{ width: `${(f.monto_usd / max) * 100}%` }} />
          </div>
        </div>
      ))}
    </div>
  );
}

function AdminPanel({ sedes, insumos, usuarioActual, onCambio }) {
  const [tab, setTab] = useState('resumen');
  const [aviso, setAviso] = useState(null);
  const avisar = (texto, tipo = 'ok') => setAviso({ texto, tipo });
  useEffect(() => {
    if (!aviso) return;
    const t = setTimeout(() => setAviso(null), 3500);
    return () => clearTimeout(t);
  }, [aviso]);

  const tabs = [
    ['resumen', 'Flujo de caja'],
    ['gastos', 'Gastos'],
    ['deudas', 'Proveedores y deudas'],
    ['productos', 'Productos y precios'],
    ['usuarios', 'Usuarios y sedes']
  ];

  return (
    <div className="space-y-4">
      {aviso && (
        <div className={`fixed top-3 left-1/2 -translate-x-1/2 z-[60] px-4 py-2.5 rounded-xl text-xs font-bold shadow-lg max-w-[92vw] ${aviso.tipo === 'error' ? 'bg-red-600 text-white' : 'bg-emerald-600 text-white'}`}>
          {aviso.texto}
        </div>
      )}
      <h2 className="text-sm font-bold text-white uppercase tracking-wider">Administración</h2>
      <div className="flex space-x-2 overflow-x-auto no-scrollbar pb-1">
        {tabs.map(([id, nombre]) => (
          <button
            key={id}
            onClick={() => setTab(id)}
            className={`whitespace-nowrap text-xs font-bold px-3.5 py-2 rounded-xl border transition ${tab === id ? 'bg-[#2563eb] border-[#2563eb] text-white' : 'bg-[#111114] border-[#222227] text-zinc-400 hover:text-white'}`}
          >
            {nombre}
          </button>
        ))}
      </div>
      {tab === 'resumen' && <FinanzasResumen sedes={sedes} />}
      {tab === 'gastos' && <GastosTab sedes={sedes} avisar={avisar} />}
      {tab === 'deudas' && <DeudasTab sedes={sedes} avisar={avisar} />}
      {tab === 'productos' && <ProductosTab insumos={insumos} avisar={avisar} onCambio={onCambio} />}
      {tab === 'usuarios' && <UsuariosTab usuarioActual={usuarioActual} avisar={avisar} onCambio={onCambio} />}
    </div>
  );
}

// ---------------------------------------------------------- FLUJO DE CAJA
function FinanzasResumen({ sedes }) {
  const [preset, setPreset] = useState('hoy');
  const [desde, setDesde] = useState(hoyCaracas());
  const [hasta, setHasta] = useState(hoyCaracas());
  const [sede, setSede] = useState('');
  const [d, setD] = useState(null);
  const [error, setError] = useState('');
  const [cargando, setCargando] = useState(false);

  const aplicarPreset = (p) => {
    setPreset(p);
    if (p === 'hoy') { setDesde(hoyCaracas()); setHasta(hoyCaracas()); }
    else if (p === '7') { setDesde(hoyCaracas(6)); setHasta(hoyCaracas()); }
    else if (p === '30') { setDesde(hoyCaracas(29)); setHasta(hoyCaracas()); }
    else if (p === 'mes') { setDesde(inicioMesCaracas()); setHasta(hoyCaracas()); }
  };

  const cargar = async () => {
    setCargando(true);
    setError('');
    const r = await apiAdmin(`/api/admin/finanzas/resumen?desde=${desde}&hasta=${hasta}${sede ? '&sede_id=' + sede : ''}`);
    if (r.success) setD(r); else setError(r.error);
    setCargando(false);
  };
  useEffect(() => { cargar(); }, [desde, hasta, sede]);

  const maxSerie = d ? Math.max(1, ...d.serie.map(x => Math.max(x.ingresos, x.egresos))) : 1;
  const sedesVenta = sedes.filter(s => !s.es_almacen);

  return (
    <div className="space-y-4">
      <div className={cardCls}>
        <div className="flex flex-wrap gap-2">
          {[['hoy', 'Hoy'], ['7', '7 días'], ['30', '30 días'], ['mes', 'Este mes']].map(([id, n]) => (
            <button key={id} onClick={() => aplicarPreset(id)} className={`text-xs font-bold px-3 py-1.5 rounded-lg border ${preset === id ? 'bg-[#2563eb] border-[#2563eb] text-white' : 'bg-[#09090b] border-[#222227] text-zinc-400'}`}>{n}</button>
          ))}
        </div>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
          <Campo label="Desde"><input type="date" value={desde} onChange={e => { setPreset('x'); setDesde(e.target.value); }} className={inputCls} /></Campo>
          <Campo label="Hasta"><input type="date" value={hasta} onChange={e => { setPreset('x'); setHasta(e.target.value); }} className={inputCls} /></Campo>
          <Campo label="Sede">
            <select value={sede} onChange={e => setSede(e.target.value)} className={inputCls}>
              <option value="">Todas las sedes</option>
              {sedesVenta.map(s => <option key={s.id} value={s.id}>{s.nombre}</option>)}
            </select>
          </Campo>
        </div>
      </div>

      {error && <div className="bg-red-950/50 border border-red-900 text-red-300 text-xs rounded-xl p-3">{error}</div>}
      {cargando && !d && <div className="text-xs text-zinc-500 py-6 text-center">Calculando…</div>}

      {d && (
        <>
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
            <Kpi titulo="Entró a caja" valor={d.ingresos_usd} tono="text-emerald-400" nota={`Ventas ${fmtUSD(d.ingresos_ventas_usd)} + fiados cobrados ${fmtUSD(d.ingresos_cobros_fiado_usd)}`} />
            <Kpi titulo="Salió de caja" valor={d.egresos_usd} tono="text-red-400" nota={`Gastos ${fmtUSD(d.gastos_usd)} + proveedores ${fmtUSD(d.pagos_proveedores_usd)}`} />
            <Kpi titulo="Flujo neto" valor={d.flujo_neto_usd} tono={d.flujo_neto_usd >= 0 ? 'text-emerald-400' : 'text-red-400'} />
            <Kpi titulo="Utilidad operativa" valor={d.utilidad_operativa_usd} tono={d.utilidad_operativa_usd >= 0 ? 'text-white' : 'text-red-400'} nota="Ventas − costo de insumos − gastos" />
          </div>
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
            <Kpi titulo="Vendido (total)" valor={d.ventas_total_usd} nota="Incluye fiados" />
            <Kpi titulo="Costo de insumos" valor={d.costo_insumos_usd} />
            <Kpi titulo="Nos deben (fiados)" valor={d.por_cobrar_usd} tono="text-amber-400" nota="Pendiente hoy, de todas las fechas" />
            <Kpi titulo="Debemos (proveedores)" valor={d.por_pagar_usd} tono="text-amber-400" nota="Pendiente hoy, de todas las fechas" />
          </div>

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            <div className={cardCls}>
              <h3 className="text-xs font-bold text-white uppercase">¿En qué se gasta?</h3>
              <ListaBarras filas={d.gastos_por_categoria} color="bg-red-500" vacio="Sin gastos registrados en este período." />
            </div>
            <div className={cardCls}>
              <h3 className="text-xs font-bold text-white uppercase">¿A quién le debemos?</h3>
              <ListaBarras filas={d.deudas_por_proveedor} color="bg-amber-500" vacio="No hay deudas pendientes con proveedores." />
            </div>
            <div className={cardCls}>
              <h3 className="text-xs font-bold text-white uppercase">¿Quién nos debe?</h3>
              <ListaBarras filas={d.fiados_por_cliente} color="bg-amber-500" vacio="No hay fiados pendientes." />
            </div>
            <div className={cardCls}>
              <h3 className="text-xs font-bold text-white uppercase">Día por día</h3>
              <div className="max-h-64 overflow-y-auto space-y-1.5 pr-1">
                {[...d.serie].reverse().map(x => (
                  <div key={x.fecha} className="text-[11px]">
                    <div className="flex justify-between text-zinc-400">
                      <span>{x.fecha.slice(5).split('-').reverse().join('/')}</span>
                      <span className="font-mono"><span className="text-emerald-400">+{fmtUSD(x.ingresos)}</span> <span className="text-red-400">−{fmtUSD(x.egresos)}</span></span>
                    </div>
                    <div className="flex space-x-1 mt-0.5">
                      <div className="h-1 bg-emerald-500 rounded-full" style={{ width: `${(x.ingresos / maxSerie) * 50}%` }} />
                      <div className="h-1 bg-red-500 rounded-full" style={{ width: `${(x.egresos / maxSerie) * 50}%` }} />
                    </div>
                  </div>
                ))}
              </div>
            </div>
          </div>
          <p className="text-[10px] text-zinc-500">
            "Entró a caja" cuenta las ventas pagadas el día de la venta y los fiados el día que se cobran. Las fechas son en hora de Venezuela.
          </p>
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------- GASTOS
function GastosTab({ sedes, avisar }) {
  const [datos, setDatos] = useState({ gastos: [], categorias: [], metodos: [], total_usd: 0 });
  const [desde, setDesde] = useState(hoyCaracas(29));
  const [hasta, setHasta] = useState(hoyCaracas());
  const [f, setF] = useState({ categoria: '', descripcion: '', monto_usd: '', metodo: 'Efectivo USD', sede_id: '', fecha: hoyCaracas() });
  const [guardando, setGuardando] = useState(false);
  const [anulandoId, setAnulandoId] = useState(null);
  const [motivo, setMotivo] = useState('');
  const [nuevaCat, setNuevaCat] = useState('');
  const [catNueva2, setCatNueva2] = useState('');
  const sedesVenta = sedes.filter(s => !s.es_almacen);

  const cargar = async () => {
    const r = await apiAdmin(`/api/admin/gastos?desde=${desde}&hasta=${hasta}`);
    if (r.success) setDatos(r); else avisar(r.error, 'error');
  };
  useEffect(() => { cargar(); }, [desde, hasta]);

  const guardar = async () => {
    setGuardando(true);
    const r = await apiAdmin('/api/admin/gastos', 'POST', {
      categoria: f.categoria === '__nueva__' ? nuevaCat.trim() : f.categoria, descripcion: f.descripcion, monto_usd: Number(f.monto_usd),
      metodo: f.metodo, sede_id: f.sede_id || null, fecha: f.fecha
    });
    setGuardando(false);
    if (!r.success) return avisar(r.error, 'error');
    avisar('Gasto registrado ✓');
    setF({ ...f, categoria: f.categoria === '__nueva__' ? nuevaCat.trim() : f.categoria, descripcion: '', monto_usd: '' });
    setNuevaCat('');
    cargar();
  };

  const anular = async (id) => {
    const r = await apiAdmin(`/api/admin/gastos/${id}/anular`, 'POST', { motivo });
    if (!r.success) return avisar(r.error, 'error');
    avisar('Gasto anulado ✓');
    setAnulandoId(null);
    setMotivo('');
    cargar();
  };

  const agregarCategoria = async () => {
    const r = await apiAdmin('/api/admin/categorias-gasto', 'POST', { nombre: catNueva2 });
    if (!r.success) return avisar(r.error, 'error');
    avisar('Categoría agregada ✓');
    setCatNueva2('');
    cargar();
  };
  const quitarCategoria = async (nombre) => {
    if (!window.confirm(`¿Quitar la categoría "${nombre}" de la lista? Los gastos ya registrados no cambian.`)) return;
    const r = await apiAdmin('/api/admin/categorias-gasto/quitar', 'POST', { nombre });
    if (!r.success) return avisar(r.error, 'error');
    cargar();
  };

  const nombreSede = (id) => (sedes.find(s => s.id === id) || {}).nombre || 'General';

  return (
    <div className="space-y-4">
      <div className={cardCls}>
        <h3 className="text-xs font-bold text-white uppercase">Registrar un gasto</h3>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
          <Campo label="Categoría">
            <select value={f.categoria} onChange={e => setF({ ...f, categoria: e.target.value })} className={inputCls}>
              <option value="">Elige…</option>
              {datos.categorias.map(c => <option key={c} value={c}>{c}</option>)}
              <option value="__nueva__">➕ Otra categoría (escribirla)…</option>
            </select>
            {f.categoria === '__nueva__' && (
              <input type="text" maxLength={40} value={nuevaCat} onChange={e => setNuevaCat(e.target.value)}
                className={inputCls + ' mt-2'} placeholder="Nombre de la categoría nueva" />
            )}
          </Campo>
          <Campo label="Monto (USD)"><input type="number" inputMode="decimal" min="0" step="0.01" value={f.monto_usd} onChange={e => setF({ ...f, monto_usd: e.target.value })} className={inputCls + ' font-mono'} placeholder="0.00" /></Campo>
          <Campo label="Fecha"><input type="date" value={f.fecha} onChange={e => setF({ ...f, fecha: e.target.value })} className={inputCls} /></Campo>
          <Campo label="Pagado con">
            <select value={f.metodo} onChange={e => setF({ ...f, metodo: e.target.value })} className={inputCls}>
              {datos.metodos.map(m => <option key={m} value={m}>{m}</option>)}
            </select>
          </Campo>
          <Campo label="Sede">
            <select value={f.sede_id} onChange={e => setF({ ...f, sede_id: e.target.value })} className={inputCls}>
              <option value="">General (todas)</option>
              {sedesVenta.map(s => <option key={s.id} value={s.id}>{s.nombre}</option>)}
            </select>
          </Campo>
          <Campo label="Detalle (opcional)"><input type="text" maxLength={200} value={f.descripcion} onChange={e => setF({ ...f, descripcion: e.target.value })} className={inputCls} placeholder="Ej: bombona de gas" /></Campo>
        </div>
        <button onClick={guardar} disabled={guardando || !f.categoria || (f.categoria === '__nueva__' && nuevaCat.trim().length < 2) || !(Number(f.monto_usd) > 0)} className={btnPri}>
          {guardando ? 'Guardando…' : 'Guardar gasto'}
        </button>
      </div>

      <div className={cardCls}>
        <h3 className="text-xs font-bold text-white uppercase">Categorías de gastos</h3>
        <div className="text-[11px] text-zinc-400">Crea las tuyas aquí o escribiéndolas al registrar un gasto. Las de fábrica no se pueden quitar.</div>
        <div className="flex flex-wrap gap-1.5">
          {datos.categorias.map(c => {
            const propia = (datos.categorias_propias || []).includes(c);
            return (
              <span key={c} className={`px-2.5 py-1 rounded-lg text-[11px] font-bold border ${propia ? 'bg-amber-500/10 border-amber-700 text-amber-300' : 'bg-[#09090b] border-[#222227] text-zinc-400'}`}>
                {c}
                {propia && <button onClick={() => quitarCategoria(c)} className="ml-1.5 text-amber-400 hover:text-red-400" title="Quitar">✕</button>}
              </span>
            );
          })}
        </div>
        <div className="flex gap-2">
          <input type="text" maxLength={40} value={catNueva2} onChange={e => setCatNueva2(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') agregarCategoria(); }}
            className={inputCls} placeholder="Nueva categoría (ej. Empaques, Publicidad…)" />
          <button onClick={agregarCategoria} disabled={catNueva2.trim().length < 2} className={btnPri}>Agregar</button>
        </div>
      </div>

      <div className={cardCls}>
        <div className="flex flex-wrap items-end justify-between gap-3">
          <h3 className="text-xs font-bold text-white uppercase">Gastos del período · <span className="text-red-400 font-mono">${fmtUSD(datos.total_usd)}</span></h3>
          <div className="flex items-center space-x-2">
            <input type="date" value={desde} onChange={e => setDesde(e.target.value)} className={inputCls + ' w-auto'} />
            <span className="text-zinc-500 text-xs">a</span>
            <input type="date" value={hasta} onChange={e => setHasta(e.target.value)} className={inputCls + ' w-auto'} />
          </div>
        </div>
        {datos.gastos.length === 0 && <div className="text-xs text-zinc-500 py-4 text-center">No hay gastos en este período.</div>}
        <div className="divide-y divide-[#222227]">
          {datos.gastos.map(g => (
            <div key={g.id} className="py-2.5 text-xs">
              <div className="flex justify-between items-start gap-2">
                <div className="min-w-0">
                  <div className="font-bold text-white">{g.categoria}{g.descripcion ? <span className="font-normal text-zinc-400"> · {g.descripcion}</span> : null}</div>
                  <div className="text-[10px] text-zinc-500">{g.fecha} · {nombreSede(g.sede_id)}{g.metodo ? ' · ' + g.metodo : ''}</div>
                </div>
                <div className="text-right shrink-0">
                  <div className="font-mono font-bold text-red-300">${fmtUSD(g.monto_usd)}</div>
                  <button onClick={() => { setAnulandoId(anulandoId === g.id ? null : g.id); setMotivo(''); }} className="text-[10px] text-zinc-500 hover:text-red-300 underline">anular</button>
                </div>
              </div>
              <div className="mt-2"><Comprobantes entidad="gasto" entidadId={g.id} /></div>
              {anulandoId === g.id && (
                <div className="flex space-x-2 mt-2">
                  <input type="text" value={motivo} onChange={e => setMotivo(e.target.value)} placeholder="Motivo de la anulación" className={inputCls} />
                  <button onClick={() => anular(g.id)} disabled={!motivo.trim()} className={btnPeligro}>Anular</button>
                </div>
              )}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------- PROVEEDORES Y DEUDAS
function DeudasTab({ sedes, avisar }) {
  const [proveedores, setProveedores] = useState([]);
  const [compras, setCompras] = useState([]);
  const [verTodas, setVerTodas] = useState(false);
  const [nuevoProv, setNuevoProv] = useState({ nombre: '', telefono: '' });
  const [compra, setCompra] = useState({ proveedor_id: '', descripcion: '', total_usd: '', pagado_usd: '', metodo: 'Efectivo USD', fecha: hoyCaracas() });
  const [abonando, setAbonando] = useState(null);
  const [abono, setAbono] = useState({ monto_usd: '', metodo: 'Efectivo USD' });
  const [ocupado, setOcupado] = useState(false);
  const metodos = ['Efectivo USD', 'Efectivo Bs', 'Pago móvil', 'Transferencia', 'Zelle', 'Otro'];

  const cargar = async () => {
    const [p, c] = await Promise.all([
      apiAdmin('/api/admin/proveedores'),
      apiAdmin('/api/admin/compras?estado=' + (verTodas ? 'todas' : 'pendientes'))
    ]);
    if (p.success) setProveedores(p.proveedores); else avisar(p.error, 'error');
    if (c.success) setCompras(c.compras); else avisar(c.error, 'error');
  };
  useEffect(() => { cargar(); }, [verTodas]);

  const totalDeuda = proveedores.reduce((a, p) => a + p.saldo_usd, 0);

  const crearProveedor = async () => {
    setOcupado(true);
    const r = await apiAdmin('/api/admin/proveedores', 'POST', nuevoProv);
    setOcupado(false);
    if (!r.success) return avisar(r.error, 'error');
    avisar('Proveedor creado ✓');
    setNuevoProv({ nombre: '', telefono: '' });
    cargar();
  };

  const crearCompra = async () => {
    setOcupado(true);
    const r = await apiAdmin('/api/admin/compras', 'POST', {
      proveedor_id: Number(compra.proveedor_id), descripcion: compra.descripcion,
      total_usd: Number(compra.total_usd), pagado_usd: Number(compra.pagado_usd) || 0,
      metodo: compra.metodo, fecha: compra.fecha
    });
    setOcupado(false);
    if (!r.success) return avisar(r.error, 'error');
    avisar('Compra registrada ✓');
    setCompra({ ...compra, descripcion: '', total_usd: '', pagado_usd: '' });
    cargar();
  };

  const abonar = async (id) => {
    setOcupado(true);
    const r = await apiAdmin(`/api/admin/compras/${id}/pago`, 'POST', { monto_usd: Number(abono.monto_usd), metodo: abono.metodo });
    setOcupado(false);
    if (!r.success) return avisar(r.error, 'error');
    avisar('Abono registrado ✓');
    setAbonando(null);
    setAbono({ monto_usd: '', metodo: 'Efectivo USD' });
    cargar();
  };

  const anular = async (id) => {
    const motivo = window.prompt('Motivo para anular esta compra:');
    if (!motivo) return;
    const r = await apiAdmin(`/api/admin/compras/${id}/anular`, 'POST', { motivo });
    if (!r.success) return avisar(r.error, 'error');
    avisar('Compra anulada ✓');
    cargar();
  };

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3">
        <Kpi titulo="Total que debemos" valor={totalDeuda} tono="text-amber-400" />
        <div className="bg-[#111114] border border-[#222227] rounded-2xl p-3">
          <div className="text-[10px] uppercase tracking-wider text-zinc-500 font-bold">Proveedores con deuda</div>
          <div className="text-lg font-black font-mono mt-1 text-white">{proveedores.filter(p => p.saldo_usd > 0).length}</div>
        </div>
      </div>

      <div className={cardCls}>
        <h3 className="text-xs font-bold text-white uppercase">Anotar una compra a un proveedor</h3>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
          <Campo label="Proveedor">
            <select value={compra.proveedor_id} onChange={e => setCompra({ ...compra, proveedor_id: e.target.value })} className={inputCls}>
              <option value="">Elige…</option>
              {proveedores.filter(p => p.vigente).map(p => <option key={p.id} value={p.id}>{p.nombre}</option>)}
            </select>
          </Campo>
          <Campo label="Total (USD)"><input type="number" inputMode="decimal" min="0" step="0.01" value={compra.total_usd} onChange={e => setCompra({ ...compra, total_usd: e.target.value })} className={inputCls + ' font-mono'} /></Campo>
          <Campo label="Pagó ahora (USD)"><input type="number" inputMode="decimal" min="0" step="0.01" value={compra.pagado_usd} onChange={e => setCompra({ ...compra, pagado_usd: e.target.value })} className={inputCls + ' font-mono'} placeholder="0 si es a crédito" /></Campo>
          <Campo label="Qué se compró"><input type="text" maxLength={200} value={compra.descripcion} onChange={e => setCompra({ ...compra, descripcion: e.target.value })} className={inputCls} placeholder="Ej: 5 paquetes de salchichas" /></Campo>
          <Campo label="Pagó con">
            <select value={compra.metodo} onChange={e => setCompra({ ...compra, metodo: e.target.value })} className={inputCls}>
              {metodos.map(m => <option key={m} value={m}>{m}</option>)}
            </select>
          </Campo>
          <Campo label="Fecha"><input type="date" value={compra.fecha} onChange={e => setCompra({ ...compra, fecha: e.target.value })} className={inputCls} /></Campo>
        </div>
        <button onClick={crearCompra} disabled={ocupado || !compra.proveedor_id || !(Number(compra.total_usd) > 0)} className={btnPri}>Guardar compra</button>
        <p className="text-[10px] text-zinc-500">Esto lleva la cuenta de lo que debes. Para sumar la mercancía al inventario, usa "Compra" en la pestaña Stock.</p>
      </div>

      <div className={cardCls}>
        <div className="flex justify-between items-center">
          <h3 className="text-xs font-bold text-white uppercase">{verTodas ? 'Todas las compras' : 'Compras con saldo pendiente'}</h3>
          <button onClick={() => setVerTodas(!verTodas)} className="text-[11px] text-[#60a5fa] underline">{verTodas ? 'Ver solo pendientes' : 'Ver todas'}</button>
        </div>
        {compras.length === 0 && <div className="text-xs text-zinc-500 py-4 text-center">{verTodas ? 'Aún no hay compras.' : 'No debes nada. 🎉'}</div>}
        <div className="space-y-2">
          {compras.map(c => (
            <div key={c.id} className="bg-[#09090b] border border-[#222227] rounded-xl p-3 text-xs space-y-2">
              <div className="flex justify-between items-start gap-2">
                <div className="min-w-0">
                  <div className="font-bold text-white">{c.proveedor}</div>
                  <div className="text-[10px] text-zinc-500">{c.fecha}{c.descripcion ? ' · ' + c.descripcion : ''}</div>
                </div>
                <div className="text-right shrink-0">
                  <div className="font-mono text-zinc-300">Total ${fmtUSD(c.total_usd)}</div>
                  <div className={`font-mono font-black ${c.saldo_usd > 0 ? 'text-amber-400' : 'text-emerald-400'}`}>{c.saldo_usd > 0 ? 'Debe $' + fmtUSD(c.saldo_usd) : 'Pagada'}</div>
                </div>
              </div>
              {c.pagos.length > 0 && (
                <div className="text-[10px] text-zinc-500">
                  Pagos: {c.pagos.map((p, i) => <span key={i}>{i ? ' · ' : ''}{p.fecha.slice(5)} ${fmtUSD(p.monto_usd)}</span>)}
                </div>
              )}
              <Comprobantes entidad="compra" entidadId={c.id} etiqueta="Facturas y comprobantes de pago" />
              {c.saldo_usd > 0 && abonando !== c.id && (
                <div className="flex space-x-2">
                  <button onClick={() => { setAbonando(c.id); setAbono({ monto_usd: String(c.saldo_usd), metodo: 'Efectivo USD' }); }} className={btnSec}>Abonar / pagar</button>
                  {c.pagos.length === 0 && <button onClick={() => anular(c.id)} className={btnPeligro}>Anular</button>}
                </div>
              )}
              {abonando === c.id && (
                <div className="flex flex-wrap items-center gap-2">
                  <input type="number" inputMode="decimal" min="0" step="0.01" value={abono.monto_usd} onChange={e => setAbono({ ...abono, monto_usd: e.target.value })} className={inputCls + ' w-28 font-mono'} />
                  <select value={abono.metodo} onChange={e => setAbono({ ...abono, metodo: e.target.value })} className={inputCls + ' w-auto'}>
                    {metodos.map(m => <option key={m} value={m}>{m}</option>)}
                  </select>
                  <button onClick={() => abonar(c.id)} disabled={ocupado || !(Number(abono.monto_usd) > 0)} className={btnPri}>Confirmar</button>
                  <button onClick={() => setAbonando(null)} className={btnSec}>Cancelar</button>
                </div>
              )}
            </div>
          ))}
        </div>
      </div>

      <div className={cardCls}>
        <h3 className="text-xs font-bold text-white uppercase">Proveedores</h3>
        <div className="flex flex-wrap gap-2">
          <input type="text" maxLength={120} value={nuevoProv.nombre} onChange={e => setNuevoProv({ ...nuevoProv, nombre: e.target.value })} placeholder="Nombre del proveedor" className={inputCls + ' flex-1 min-w-[160px]'} />
          <input type="text" maxLength={40} value={nuevoProv.telefono} onChange={e => setNuevoProv({ ...nuevoProv, telefono: e.target.value })} placeholder="Teléfono" className={inputCls + ' w-40'} />
          <button onClick={crearProveedor} disabled={ocupado || !nuevoProv.nombre.trim()} className={btnPri}>Agregar</button>
        </div>
        <div className="divide-y divide-[#222227]">
          {proveedores.length === 0 && <div className="text-xs text-zinc-500 py-3">Todavía no hay proveedores.</div>}
          {proveedores.map(p => (
            <div key={p.id} className="py-2 flex justify-between items-center text-xs">
              <div>
                <span className={p.vigente ? 'text-white font-bold' : 'text-zinc-500 line-through'}>{p.nombre}</span>
                {p.telefono && <span className="text-zinc-500"> · {p.telefono}</span>}
              </div>
              <span className={`font-mono font-bold ${p.saldo_usd > 0 ? 'text-amber-400' : 'text-zinc-500'}`}>{p.saldo_usd > 0 ? 'Debe $' + fmtUSD(p.saldo_usd) : 'Al día'}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------- PRODUCTOS, PRECIOS Y COMBOS
function ProductosTab({ insumos, avisar, onCambio }) {
  const [productos, setProductos] = useState([]);
  const [tipos, setTipos] = useState(['INDIVIDUAL', 'COMBO', 'BEBIDA', 'EXTRA']);
  const [precios, setPrecios] = useState({});
  const [ed, setEd] = useState(null);
  const [ocupado, setOcupado] = useState(false);

  const cargar = async () => {
    const r = await apiAdmin('/api/admin/productos');
    if (!r.success) return avisar(r.error, 'error');
    setProductos(r.productos);
    setTipos(r.tipos);
    setPrecios({});
  };
  useEffect(() => { cargar(); }, []);

  const costoInsumo = (id) => { const i = insumos.find(x => Number(x.id) === Number(id)); return i ? Number(i.costo_unitario_usd) : 0; };
  const prod = (id) => productos.find(p => Number(p.id) === Number(id));

  const costoEditor = () => {
    if (!ed) return 0;
    if (ed.modo === 'combo') return ed.componentes.reduce((a, c) => a + ((prod(c.producto_id) || {}).costo_usd || 0) * (Number(c.cantidad) || 0), 0);
    return ed.receta.reduce((a, r) => a + costoInsumo(r.insumo_id) * (Number(r.cantidad) || 0), 0);
  };
  const precioSugeridoCombo = () => ed && ed.modo === 'combo'
    ? ed.componentes.reduce((a, c) => a + ((prod(c.producto_id) || {}).precio_venta_usd || 0) * (Number(c.cantidad) || 0), 0) : 0;

  const nuevo = () => setEd({ id: null, nombre: '', tipo: 'INDIVIDUAL', precio: '', activo: true, modo: 'insumos', receta: [{ insumo_id: '', cantidad: 1 }], componentes: [{ producto_id: '', cantidad: 1 }] });
  const editar = (p) => setEd({
    id: p.id, nombre: p.nombre, tipo: p.tipo, precio: String(p.precio_venta_usd), activo: p.activo,
    modo: p.componentes.length ? 'combo' : 'insumos',
    receta: p.receta.length ? p.receta.map(r => ({ ...r })) : [{ insumo_id: '', cantidad: 1 }],
    componentes: p.componentes.length ? p.componentes.map(c => ({ ...c })) : [{ producto_id: '', cantidad: 1 }]
  });

  const guardarPrecio = async (p) => {
    const r = await apiAdmin(`/api/admin/productos/${p.id}`, 'PATCH', { precio_venta_usd: Number(precios[p.id]) });
    if (!r.success) return avisar(r.error, 'error');
    avisar(`Precio de "${p.nombre}" actualizado ✓`);
    await cargar();
    onCambio && onCambio();
  };

  const alternar = async (p) => {
    const r = await apiAdmin(`/api/admin/productos/${p.id}`, 'PATCH', { activo: !p.activo });
    if (!r.success) return avisar(r.error, 'error');
    avisar(p.activo ? 'Producto oculto de la caja ✓' : 'Producto visible en la caja ✓');
    await cargar();
    onCambio && onCambio();
  };

  const guardar = async () => {
    const cuerpo = { nombre: ed.nombre, tipo: ed.tipo, precio_venta_usd: Number(ed.precio), activo: ed.activo };
    if (ed.modo === 'combo') cuerpo.componentes = ed.componentes.filter(c => c.producto_id).map(c => ({ producto_id: Number(c.producto_id), cantidad: Number(c.cantidad) || 1 }));
    else cuerpo.receta = ed.receta.filter(r => r.insumo_id).map(r => ({ insumo_id: Number(r.insumo_id), cantidad: Number(r.cantidad) }));
    setOcupado(true);
    const r = ed.id ? await apiAdmin(`/api/admin/productos/${ed.id}`, 'PATCH', cuerpo) : await apiAdmin('/api/admin/productos', 'POST', cuerpo);
    setOcupado(false);
    if (!r.success) return avisar(r.error, 'error');
    avisar(ed.id ? 'Producto actualizado ✓' : 'Producto creado ✓');
    setEd(null);
    await cargar();
    onCambio && onCambio();
  };

  const costo = costoEditor();
  const precioEd = Number(ed && ed.precio) || 0;
  const margenEd = precioEd > 0 ? ((precioEd - costo) / precioEd) * 100 : 0;
  const base = productos.filter(p => (!ed || p.id !== ed.id) && !(p.componentes && p.componentes.length));

  return (
    <div className="space-y-4">
      <div className="flex justify-between items-center">
        <p className="text-xs text-zinc-400">Cambia precios al instante, crea productos y arma combos con otros productos.</p>
        <button onClick={nuevo} className={btnPri}>+ Nuevo producto</button>
      </div>

      {ed && (
        <div className={cardCls + ' border-[#2563eb]/60'}>
          <h3 className="text-xs font-bold text-white uppercase">{ed.id ? 'Editar producto' : 'Nuevo producto'}</h3>
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
            <Campo label="Nombre"><input type="text" maxLength={120} value={ed.nombre} onChange={e => setEd({ ...ed, nombre: e.target.value })} className={inputCls} /></Campo>
            <Campo label="Tipo">
              <select value={ed.tipo} onChange={e => setEd({ ...ed, tipo: e.target.value, modo: e.target.value === 'COMBO' ? ed.modo : 'insumos' })} className={inputCls}>
                {tipos.map(t => <option key={t} value={t}>{t}</option>)}
              </select>
            </Campo>
            <Campo label="Precio de venta (USD)"><input type="number" inputMode="decimal" min="0" step="0.01" value={ed.precio} onChange={e => setEd({ ...ed, precio: e.target.value })} className={inputCls + ' font-mono'} /></Campo>
          </div>

          {ed.tipo === 'COMBO' && (
            <div className="flex space-x-2 text-xs">
              <button onClick={() => setEd({ ...ed, modo: 'combo' })} className={`px-3 py-1.5 rounded-lg border font-bold ${ed.modo === 'combo' ? 'bg-[#2563eb] border-[#2563eb] text-white' : 'bg-[#09090b] border-[#222227] text-zinc-400'}`}>Armar con productos</button>
              <button onClick={() => setEd({ ...ed, modo: 'insumos' })} className={`px-3 py-1.5 rounded-lg border font-bold ${ed.modo === 'insumos' ? 'bg-[#2563eb] border-[#2563eb] text-white' : 'bg-[#09090b] border-[#222227] text-zinc-400'}`}>Receta manual</button>
            </div>
          )}

          {ed.modo === 'combo' ? (
            <div className="space-y-2">
              <div className="text-[11px] text-zinc-400">Productos que incluye. Si cambias la receta de uno de ellos, el combo se recalcula solo.</div>
              {ed.componentes.map((c, i) => (
                <div key={i} className="flex space-x-2">
                  <select value={c.producto_id} onChange={e => { const l = [...ed.componentes]; l[i] = { ...c, producto_id: e.target.value }; setEd({ ...ed, componentes: l }); }} className={inputCls}>
                    <option value="">Elige un producto…</option>
                    {base.map(p => <option key={p.id} value={p.id}>{p.nombre}</option>)}
                  </select>
                  <input type="number" min="1" step="1" value={c.cantidad} onChange={e => { const l = [...ed.componentes]; l[i] = { ...c, cantidad: e.target.value }; setEd({ ...ed, componentes: l }); }} className={inputCls + ' w-20 font-mono'} />
                  <button onClick={() => setEd({ ...ed, componentes: ed.componentes.filter((_, k) => k !== i) })} className={btnSec}>✕</button>
                </div>
              ))}
              <button onClick={() => setEd({ ...ed, componentes: [...ed.componentes, { producto_id: '', cantidad: 1 }] })} className={btnSec}>+ Agregar producto</button>
              {precioSugeridoCombo() > 0 && <div className="text-[11px] text-zinc-400">Si se vendieran por separado: <b className="text-white font-mono">${fmtUSD(precioSugeridoCombo())}</b></div>}
            </div>
          ) : (
            <div className="space-y-2">
              <div className="text-[11px] text-zinc-400">Insumos que se descuentan del inventario al vender una unidad.</div>
              {ed.receta.map((r, i) => (
                <div key={i} className="flex space-x-2">
                  <select value={r.insumo_id} onChange={e => { const l = [...ed.receta]; l[i] = { ...r, insumo_id: e.target.value }; setEd({ ...ed, receta: l }); }} className={inputCls}>
                    <option value="">Elige un insumo…</option>
                    {insumos.map(x => <option key={x.id} value={x.id}>{x.nombre} ({x.unidad})</option>)}
                  </select>
                  <input type="number" min="0" step="0.01" value={r.cantidad} onChange={e => { const l = [...ed.receta]; l[i] = { ...r, cantidad: e.target.value }; setEd({ ...ed, receta: l }); }} className={inputCls + ' w-20 font-mono'} />
                  <button onClick={() => setEd({ ...ed, receta: ed.receta.filter((_, k) => k !== i) })} className={btnSec}>✕</button>
                </div>
              ))}
              <button onClick={() => setEd({ ...ed, receta: [...ed.receta, { insumo_id: '', cantidad: 1 }] })} className={btnSec}>+ Agregar insumo</button>
            </div>
          )}

          <div className="grid grid-cols-3 gap-3 bg-[#09090b] border border-[#222227] rounded-xl p-3 text-center">
            <div><div className="text-[10px] text-zinc-500 uppercase font-bold">Costo</div><div className="font-mono font-black text-white">${fmtUSD(costo)}</div></div>
            <div><div className="text-[10px] text-zinc-500 uppercase font-bold">Ganancia</div><div className={`font-mono font-black ${precioEd - costo >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>${fmtUSD(precioEd - costo)}</div></div>
            <div><div className="text-[10px] text-zinc-500 uppercase font-bold">Margen</div><div className={`font-mono font-black ${margenEd >= 30 ? 'text-emerald-400' : margenEd >= 0 ? 'text-amber-400' : 'text-red-400'}`}>{margenEd.toFixed(0)}%</div></div>
          </div>

          <label className="flex items-center space-x-2 text-xs text-zinc-300">
            <input type="checkbox" checked={ed.activo} onChange={e => setEd({ ...ed, activo: e.target.checked })} />
            <span>Visible en la caja</span>
          </label>
          <div className="flex space-x-2">
            <button onClick={guardar} disabled={ocupado || !ed.nombre.trim() || !(Number(ed.precio) >= 0) || ed.precio === ''} className={btnPri}>{ocupado ? 'Guardando…' : 'Guardar producto'}</button>
            <button onClick={() => setEd(null)} className={btnSec}>Cancelar</button>
          </div>
        </div>
      )}

      {tipos.map(tipo => {
        const lista = productos.filter(p => p.tipo === tipo);
        if (!lista.length) return null;
        return (
          <div key={tipo} className={cardCls}>
            <h3 className="text-xs font-bold text-zinc-400 uppercase">{tipo}</h3>
            <div className="divide-y divide-[#222227]">
              {lista.map(p => {
                const cambiado = precios[p.id] !== undefined && Number(precios[p.id]) !== p.precio_venta_usd;
                return (
                  <div key={p.id} className={`py-2.5 flex flex-wrap items-center gap-2 text-xs ${p.activo ? '' : 'opacity-50'}`}>
                    <div className="flex-1 min-w-[150px]">
                      <div className="font-bold text-white">{p.nombre}{p.componentes.length ? <span className="ml-1 text-[9px] text-[#60a5fa] border border-[#2563eb]/50 rounded px-1">armado</span> : null}</div>
                      <div className="text-[10px] text-zinc-500 font-mono">Costo ${fmtUSD(p.costo_usd)} · margen <span className={p.margen_pct >= 30 ? 'text-emerald-400' : p.margen_pct >= 0 ? 'text-amber-400' : 'text-red-400'}>{p.margen_pct.toFixed(0)}%</span></div>
                    </div>
                    <div className="flex items-center space-x-1">
                      <span className="text-zinc-500">$</span>
                      <input type="number" inputMode="decimal" min="0" step="0.01" value={precios[p.id] !== undefined ? precios[p.id] : p.precio_venta_usd} onChange={e => setPrecios({ ...precios, [p.id]: e.target.value })} className={inputCls + ' w-20 font-mono'} />
                      {cambiado && <button onClick={() => guardarPrecio(p)} className={btnPri + ' !px-3 !py-2'}>OK</button>}
                    </div>
                    <button onClick={() => editar(p)} className={btnSec}>Editar</button>
                    <button onClick={() => alternar(p)} className={btnSec}>{p.activo ? 'Ocultar' : 'Mostrar'}</button>
                  </div>
                );
              })}
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ---------------------------------------------------------- USUARIOS Y SEDES
function UsuariosTab({ usuarioActual, avisar, onCambio }) {
  const [usuarios, setUsuarios] = useState([]);
  const [sedes, setSedes] = useState([]);
  const [ed, setEd] = useState(null);
  const [nuevaSede, setNuevaSede] = useState('');
  const [editSede, setEditSede] = useState(null);
  const [ocupado, setOcupado] = useState(false);

  const cargar = async () => {
    const [u, s] = await Promise.all([apiAdmin('/api/admin/usuarios'), apiAdmin('/api/admin/sedes')]);
    if (u.success) setUsuarios(u.usuarios); else avisar(u.error, 'error');
    if (s.success) setSedes(s.sedes); else avisar(s.error, 'error');
  };
  useEffect(() => { cargar(); }, []);

  const nombreSede = (id) => (sedes.find(s => s.id === id) || {}).nombre || '—';
  const sedesVenta = sedes.filter(s => !s.es_almacen && s.activa);

  const guardarUsuario = async () => {
    const cuerpo = { nombre: ed.nombre, username: ed.username, rol: ed.rol, sede_id: ed.rol === 'cajero' ? Number(ed.sede_id) || null : null };
    if (ed.pin) cuerpo.pin = ed.pin;
    setOcupado(true);
    let r;
    if (ed.id) { cuerpo.activo = ed.activo; r = await apiAdmin(`/api/admin/usuarios/${ed.id}`, 'PATCH', cuerpo); }
    else { cuerpo.pin = ed.pin; r = await apiAdmin('/api/admin/usuarios', 'POST', cuerpo); }
    setOcupado(false);
    if (!r.success) return avisar(r.error, 'error');
    avisar(ed.id ? 'Usuario actualizado ✓' : 'Usuario creado ✓');
    setEd(null);
    await cargar();
    onCambio && onCambio();
  };

  const crearSede = async () => {
    setOcupado(true);
    const r = await apiAdmin('/api/admin/sedes', 'POST', { nombre: nuevaSede });
    setOcupado(false);
    if (!r.success) return avisar(r.error, 'error');
    avisar('Sede creada ✓');
    setNuevaSede('');
    await cargar();
    onCambio && onCambio();
  };

  const guardarSede = async (s, cambios) => {
    const r = await apiAdmin(`/api/admin/sedes/${s.id}`, 'PATCH', cambios);
    if (!r.success) return avisar(r.error, 'error');
    avisar('Sede actualizada ✓');
    setEditSede(null);
    await cargar();
    onCambio && onCambio();
  };

  const pinOk = !ed || (ed.id ? (!ed.pin || /^\d{6,12}$/.test(ed.pin)) : /^\d{6,12}$/.test(ed.pin || ''));

  return (
    <div className="space-y-4">
      <div className={cardCls}>
        <div className="flex justify-between items-center">
          <h3 className="text-xs font-bold text-white uppercase">Usuarios</h3>
          <button onClick={() => setEd({ id: null, nombre: '', username: '', rol: 'cajero', sede_id: sedesVenta[0] ? String(sedesVenta[0].id) : '', pin: '', activo: true })} className={btnPri}>+ Nuevo usuario</button>
        </div>

        {ed && (
          <div className="bg-[#09090b] border border-[#2563eb]/60 rounded-xl p-3 space-y-3">
            <div className="text-xs font-bold text-white">{ed.id ? 'Editar usuario' : 'Nuevo usuario'}</div>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <Campo label="Nombre"><input type="text" maxLength={120} value={ed.nombre} onChange={e => setEd({ ...ed, nombre: e.target.value })} className={inputCls} /></Campo>
              <Campo label="Usuario (para entrar)"><input type="text" maxLength={60} autoCapitalize="none" value={ed.username} onChange={e => setEd({ ...ed, username: e.target.value.toLowerCase() })} className={inputCls + ' font-mono'} /></Campo>
              <Campo label="Rol">
                <select value={ed.rol} onChange={e => setEd({ ...ed, rol: e.target.value })} className={inputCls}>
                  <option value="cajero">Cajero</option>
                  <option value="superusuario">Superusuario (dueño)</option>
                </select>
              </Campo>
              {ed.rol === 'cajero' && (
                <Campo label="Sede donde trabaja">
                  <select value={ed.sede_id || ''} onChange={e => setEd({ ...ed, sede_id: e.target.value })} className={inputCls}>
                    <option value="">Elige…</option>
                    {sedesVenta.map(s => <option key={s.id} value={s.id}>{s.nombre}</option>)}
                  </select>
                </Campo>
              )}
              <Campo label={ed.id ? 'Nuevo PIN (déjalo vacío para no cambiarlo)' : 'PIN (6 a 12 números)'}>
                <input type="password" inputMode="numeric" maxLength={12} autoComplete="new-password" value={ed.pin} onChange={e => setEd({ ...ed, pin: e.target.value.replace(/\D/g, '') })} className={inputCls + ' font-mono'} />
              </Campo>
            </div>
            {ed.id && ed.id !== usuarioActual.id && (
              <label className="flex items-center space-x-2 text-xs text-zinc-300">
                <input type="checkbox" checked={ed.activo} onChange={e => setEd({ ...ed, activo: e.target.checked })} />
                <span>Puede entrar al sistema</span>
              </label>
            )}
            {!pinOk && <div className="text-[11px] text-amber-400">El PIN debe tener entre 6 y 12 números.</div>}
            <div className="flex space-x-2">
              <button onClick={guardarUsuario} disabled={ocupado || !pinOk || !ed.nombre.trim() || !ed.username.trim() || (ed.rol === 'cajero' && !ed.sede_id)} className={btnPri}>{ocupado ? 'Guardando…' : 'Guardar'}</button>
              <button onClick={() => setEd(null)} className={btnSec}>Cancelar</button>
            </div>
          </div>
        )}

        <div className="divide-y divide-[#222227]">
          {usuarios.map(u => (
            <div key={u.id} className={`py-2.5 flex items-center justify-between gap-2 text-xs ${u.activo ? '' : 'opacity-50'}`}>
              <div className="min-w-0">
                <div className="font-bold text-white truncate">{u.nombre}{u.id === usuarioActual.id ? <span className="text-zinc-500 font-normal"> (tú)</span> : null}</div>
                <div className="text-[10px] text-zinc-500 font-mono">{u.username} · {u.rol === 'superusuario' ? 'Superusuario' : 'Cajero · ' + nombreSede(u.sede_id)}{u.activo ? '' : ' · desactivado'}</div>
              </div>
              <button onClick={() => setEd({ id: u.id, nombre: u.nombre, username: u.username, rol: u.rol, sede_id: u.sede_id ? String(u.sede_id) : '', pin: '', activo: u.activo })} className={btnSec}>Editar</button>
            </div>
          ))}
        </div>
      </div>

      <div className={cardCls}>
        <h3 className="text-xs font-bold text-white uppercase">Sedes</h3>
        <div className="flex space-x-2">
          <input type="text" maxLength={120} value={nuevaSede} onChange={e => setNuevaSede(e.target.value)} placeholder="Nombre de la nueva sede" className={inputCls} />
          <button onClick={crearSede} disabled={ocupado || !nuevaSede.trim()} className={btnPri}>Agregar</button>
        </div>
        <div className="divide-y divide-[#222227]">
          {sedes.map(s => (
            <div key={s.id} className={`py-2.5 flex items-center justify-between gap-2 text-xs ${s.activa ? '' : 'opacity-50'}`}>
              {editSede && editSede.id === s.id ? (
                <div className="flex flex-1 space-x-2">
                  <input type="text" value={editSede.nombre} onChange={e => setEditSede({ ...editSede, nombre: e.target.value })} className={inputCls} />
                  <button onClick={() => guardarSede(s, { nombre: editSede.nombre })} className={btnPri}>OK</button>
                  <button onClick={() => setEditSede(null)} className={btnSec}>✕</button>
                </div>
              ) : (
                <>
                  <div className="font-bold text-white">{s.nombre}{s.es_almacen ? <span className="ml-2 text-[9px] text-zinc-400 border border-zinc-700 rounded px-1">ALMACÉN</span> : null}{s.activa ? '' : <span className="ml-2 text-[9px] text-zinc-500">(inactiva)</span>}</div>
                  <div className="flex space-x-2">
                    <button onClick={() => setEditSede({ id: s.id, nombre: s.nombre })} className={btnSec}>Renombrar</button>
                    {!s.es_almacen && <button onClick={() => guardarSede(s, { activa: !s.activa })} className={btnSec}>{s.activa ? 'Desactivar' : 'Activar'}</button>}
                  </div>
                </>
              )}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

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

  // Pedidos que llegan desde el menú público
  const [pedidosWeb, setPedidosWeb] = useState([]);
  const [avisoWeb] = useState({ activos: 0 });
  const cargarPedidosWeb = async () => {
    const r = await apiAdmin('/api/pedidos-web');
    if (r.success) {
      setPedidosWeb(r.pedidos);
      const activos = r.pedidos.filter(p => p.estado === 'PENDIENTE_PAGO' || p.estado === 'PAGO_REPORTADO').length;
      if (activos > avisoWeb.activos) sonarAviso();
      avisoWeb.activos = activos;
    }
  };
  useEffect(() => {
    cargarPedidosWeb();
    const t = setInterval(cargarPedidosWeb, 15000);
    return () => clearInterval(t);
  }, []);
  const confirmarPedidoWeb = async (p) => {
    const aviso = p.estado === 'PAGO_REPORTADO'
      ? `¿Confirmas que recibiste el pago del pedido #${p.id}?`
      : `El cliente aún no registró su pago. ¿Confirmas que ya lo recibiste por otra vía?`;
    if (!window.confirm(aviso)) return;
    const r = await apiAdmin(`/api/pedidos-web/${p.id}/confirmar`, 'POST', {});
    if (!r.success) alert(r.error);
    await cargarDesdeBackend();
    await cargarPedidosWeb();
  };
  const rechazarPedidoWeb = async (p) => {
    const motivo = window.prompt('¿Por qué lo rechazas? (opcional). La mercancía se libera de inmediato.', '');
    if (motivo === null) return;
    const r = await apiAdmin(`/api/pedidos-web/${p.id}/rechazar`, 'POST', { motivo });
    if (!r.success) alert(r.error);
    await cargarPedidosWeb();
  };
  const pedidosWebActivos = pedidosWeb.filter(p => p.estado === 'PENDIENTE_PAGO' || p.estado === 'PAGO_REPORTADO');

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
          setGastosOperativosUSD(data.config.gastos_operativos_fijos || 0);
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
  const [referenciaPago, setReferenciaPago] = useState('');
  const [fotoComprobante, setFotoComprobante] = useState(null);
  const [ventaReciente, setVentaReciente] = useState(null);
  const [esFiado, setEsFiado] = useState(false);
  const [metodoCobroFiado, setMetodoCobroFiado] = useState('Efectivo USD');
  const [cobrandoId, setCobrandoId] = useState(null);
  const recordarDeuda = (c) => {
    const msg = `Hola ${c.nombre} 👋, te escribimos para recordarte que tienes un saldo pendiente de *Bs. ${c.bs.toLocaleString('es-VE', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}* ` +
      `(${c.n} ${c.n > 1 ? 'cuentas' : 'cuenta'}). ¡Gracias por tu compra!`;
    window.open(`https://wa.me/${telWhatsApp(c.telefono)}?text=${encodeURIComponent(msg)}`, '_blank');
  };

  const cobrarFiado = async (ord) => {
    setCobrandoId(ord.id);
    try {
      const res = await fetch('/api/orden/estado', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ orden_id: ord.id, estado_pago: 'PAGADA', metodo_cobro: metodoCobroFiado })
      });
      const d = await res.json().catch(() => ({}));
      if (res.ok && d.success) {
        setOrdenes(prev => prev.map(o => (o.id === ord.id ? { ...o, estado_pago: 'PAGADA' } : o)));
      } else {
        alert(d.error || 'No se pudo registrar el cobro.');
      }
    } catch (e) {
      alert('Sin conexión: el cobro no se guardó. Inténtalo de nuevo.');
    }
    setCobrandoId(null);
  };
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
  const hoyStr = hoyCaracas();
  const hace7DiasStr = hoyCaracas(7);

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
    const hoy = hoyCaracas();
    if (filtroPresetFecha === 'hoy') {
      setFechaInicioReporte(hoy);
      setFechaFinReporte(hoy);
    } else if (filtroPresetFecha === 'ayer') {
      const ayer = hoyCaracas(1);
      setFechaInicioReporte(ayer);
      setFechaFinReporte(ayer);
    } else if (filtroPresetFecha === '7dias') {
      const d7 = hoyCaracas(7);
      setFechaInicioReporte(d7);
      setFechaFinReporte(hoy);
    } else if (filtroPresetFecha === '30dias') {
      const d30 = hoyCaracas(30);
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
    if (esFiado && clienteNombre.trim().length < 2) {
      alert('Para vender fiado escribe el nombre del cliente.');
      return;
    }

    const fotoParaSubir = fotoComprobante;
    const referenciaParaGuardar = referenciaPago.trim();
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
      recibido_usd: esFiado ? 0 : cobroRecibidoUSD,
      recibido_bs: esFiado ? 0 : cobroRecibidoBS,
      vuelto_usd: esFiado ? 0 : cobroVueltoUSD,
      vuelto_bs: esFiado ? 0 : cobroVueltoBS,
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
      fecha_corta: hoyCaracas()
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
        recibido_usd: nuevaOrden.recibido_usd,
        recibido_bs: nuevaOrden.recibido_bs,
        vuelto_usd: nuevaOrden.vuelto_usd,
        vuelto_bs: nuevaOrden.vuelto_bs,
        estado_pago: nuevaOrden.estado_pago,
        notas_cocina: notasCocina,
        referencia_pago: referenciaParaGuardar,
        items: carrito.map(c => ({ producto_id: c.producto.id, cantidad: c.cantidad, modificadores: c.modificadores }))
      })
    }).then(r => r.json()).then(d => {
      if (d && d.success === false) {
        alert('No se pudo guardar la orden: ' + (d.error || 'error desconocido'));
      } else if (d && d.orden_id) {
        setVentaReciente({
          id: d.orden_id,
          cliente: nuevaOrden.cliente_nombre,
          telefono: nuevaOrden.cliente_telefono,
          totalBs: totalCarritoUSD * tasaActiva,
          fiado: nuevaOrden.estado_pago === 'FIADO',
          comprobante: fotoParaSubir ? 'subiendo' : null
        });
        if (fotoParaSubir) {
          subirComprobante('orden', d.orden_id, fotoParaSubir).then(up => {
            setVentaReciente(v => (v && v.id === d.orden_id ? { ...v, comprobante: up.success ? 'ok' : (up.error || 'error') } : v));
            cargarDesdeBackend();
          });
        }
      }
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
    setReferenciaPago('');
    setFotoComprobante(null);
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
              <button
                onClick={() => setTabCocina('webs')}
                className={`pb-2.5 px-4 flex items-center space-x-2 transition ${
                  tabCocina === 'webs'
                    ? 'border-b-2 border-[#2563eb] text-[#2563eb]'
                    : 'text-zinc-400 hover:text-white'
                }`}
              >
                <span>🌐 Pedidos web ({pedidosWebActivos.length})</span>
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

                      <AccionesOrden ord={ord} />

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

            {tabCocina === 'webs' && (
              <div className="space-y-3">
                {pedidosWebActivos.length === 0 && (
                  <div className="py-12 text-center text-zinc-500 text-xs font-medium">No hay pedidos web esperando pago ni verificación.</div>
                )}
                {pedidosWebActivos.map(p => (
                  <div key={p.id} className={`bg-[#111114] border rounded-2xl p-3.5 space-y-2 ${p.estado === 'PAGO_REPORTADO' ? 'border-emerald-700' : 'border-[#222227]'}`}>
                    <div className="flex items-start justify-between gap-2">
                      <div className="min-w-0">
                        <div className="text-sm font-bold text-white truncate">#{p.id} • {p.cliente}</div>
                        <div className="text-[10px] text-zinc-400">
                          {p.telefono} • {p.creado}{usuarioActual.rol === 'superusuario' && p.sede_nombre ? ` • ${p.sede_nombre}` : ''}
                        </div>
                      </div>
                      <div className="text-right shrink-0">
                        <div className="text-base font-mono font-black text-amber-400">Bs. {p.total_bs.toFixed(2)}</div>
                        <div className="text-[10px] text-zinc-500">${p.total_usd.toFixed(2)}</div>
                      </div>
                    </div>
                    <div className="text-[11px] text-zinc-300">{p.items.map(i => `${i.cantidad}× ${i.nombre}`).join(', ')}</div>
                    {p.notas && <div className="text-[11px] text-amber-300">📝 {p.notas}</div>}

                    {p.estado === 'PENDIENTE_PAGO' ? (
                      <div className="text-[11px] text-amber-400 font-bold">
                        ⏳ Esperando el pago • vence en <Cuenta key={`${p.id}-${p.segundos_restantes}`} segundos={p.segundos_restantes} />
                      </div>
                    ) : (
                      <div className="text-[11px] bg-emerald-950/40 border border-emerald-900 rounded-lg p-2 space-y-0.5">
                        <div className="text-emerald-300 font-bold">💸 Pago reportado a las {p.reportado} — verifícalo en tu banco</div>
                        <div className="text-zinc-300">Método: {p.pago_metodo} • Ref: <span className="font-mono">{p.referencia || '(solo foto)'}</span> • Reportó: Bs. {p.monto_reportado_bs.toFixed(2)}</div>
                        {p.referencia_repetida && <div className="text-red-400 font-bold">⚠ Esta referencia ya se usó en otro pedido</div>}
                        {Math.abs(p.monto_reportado_bs - p.total_bs) > 0.5 && p.monto_reportado_bs > 0 && (
                          <div className="text-amber-400 font-bold">⚠ El monto reportado no coincide con el total</div>
                        )}
                      </div>
                    )}

                    <Comprobantes entidad="orden" entidadId={p.id} cantidad={p.archivos.length} etiqueta="Comprobante del cliente" />

                    <div className="flex gap-2 pt-1">
                      <button onClick={() => confirmarPedidoWeb(p)} className="flex-1 py-2.5 bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-bold rounded-xl">
                        ✔ Confirmar pago
                      </button>
                      <button onClick={() => rechazarPedidoWeb(p)} className="px-4 py-2.5 bg-red-950/60 border border-red-900 text-red-300 text-xs font-bold rounded-xl">
                        Rechazar
                      </button>
                    </div>
                  </div>
                ))}

                {pedidosWeb.filter(p => p.estado === 'EXPIRADA' || p.estado === 'RECHAZADA').length > 0 && (
                  <div className="pt-2 space-y-1.5">
                    <div className="text-[10px] uppercase font-bold text-zinc-500">Cerrados en las últimas 12 horas</div>
                    {pedidosWeb.filter(p => p.estado === 'EXPIRADA' || p.estado === 'RECHAZADA').map(p => (
                      <div key={p.id} className="flex justify-between text-[11px] bg-[#0d0d10] border border-[#1c1c21] rounded-lg p-2 text-zinc-500">
                        <span>#{p.id} • {p.cliente} • {p.items.map(i => `${i.cantidad}× ${i.nombre}`).join(', ')}</span>
                        <span>{p.estado === 'EXPIRADA' ? 'Venció, mercancía liberada' : 'Rechazado'}</span>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}

            {tabCocina === 'fiados' && (
              <div className="space-y-3">
                {ordenes.filter(o => o.estado_pago === 'FIADO').length > 0 && (
                  <div className="flex flex-wrap items-center justify-between gap-2 bg-[#111114] border border-[#222227] rounded-2xl p-3 text-xs">
                    <span className="text-zinc-400">
                      Total por cobrar:{' '}
                      <b className="text-amber-400 font-mono">
                        ${ordenes.filter(o => o.estado_pago === 'FIADO').reduce((a, o) => a + o.total_venta_usd, 0).toFixed(2)}
                      </b>
                    </span>
                    <label className="flex items-center space-x-2 text-zinc-400">
                      <span>Cobrar con:</span>
                      <select
                        value={metodoCobroFiado}
                        onChange={e => setMetodoCobroFiado(e.target.value)}
                        className="bg-[#09090b] border border-[#222227] rounded-lg p-1.5 text-white text-xs"
                      >
                        {['Efectivo USD', 'Efectivo Bs', 'Pago móvil', 'Transferencia', 'Zelle'].map(m => (
                          <option key={m} value={m}>{m}</option>
                        ))}
                      </select>
                    </label>
                  </div>
                )}

                {(() => {
                  const grupos = {};
                  ordenes.filter(o => o.estado_pago === 'FIADO').forEach(o => {
                    const k = String(o.cliente_nombre || 'Sin nombre').trim().toLowerCase();
                    if (!grupos[k]) grupos[k] = { nombre: o.cliente_nombre || 'Sin nombre', telefono: '', total: 0, bs: 0, n: 0 };
                    grupos[k].total += o.total_venta_usd;
                    grupos[k].bs += o.total_venta_usd * o.tasa_aplicada;
                    grupos[k].n += 1;
                    if (o.cliente_telefono) grupos[k].telefono = o.cliente_telefono;
                  });
                  const lista = Object.values(grupos).sort((a, b) => b.total - a.total);
                  if (lista.length === 0) return null;
                  return (
                    <div className="bg-[#111114] border border-amber-900/60 rounded-2xl p-3.5 space-y-2">
                      <div className="text-[11px] font-bold text-zinc-300 uppercase tracking-wider">Quién te debe</div>
                      {lista.map(c => (
                        <div key={c.nombre} className="flex items-center justify-between gap-2 text-xs">
                          <div className="min-w-0">
                            <div className="font-bold text-white truncate">{c.nombre}</div>
                            <div className="text-[10px] text-zinc-500">{c.n} {c.n > 1 ? 'cuentas' : 'cuenta'}{c.telefono ? ` · ${c.telefono}` : ' · sin teléfono'}</div>
                          </div>
                          <div className="flex items-center gap-2 shrink-0">
                            <div className="text-right">
                              <div className="font-mono font-black text-amber-400">${c.total.toFixed(2)}</div>
                              <div className="text-[10px] text-zinc-500 font-mono">Bs {c.bs.toFixed(2)}</div>
                            </div>
                            <button onClick={() => recordarDeuda(c)} className="px-2.5 py-1.5 bg-emerald-700 hover:bg-emerald-600 text-white text-[11px] font-bold rounded-lg" title="Recordar por WhatsApp">📲 Recordar</button>
                          </div>
                        </div>
                      ))}
                    </div>
                  );
                })()}

                {ordenes
                  .filter(o => o.estado_pago === 'FIADO')
                  .sort((a, b) => String(a.fecha_corta).localeCompare(String(b.fecha_corta)))
                  .map(ord => {
                    const dias = Math.max(0, Math.floor((Date.parse(hoyCaracas()) - Date.parse(ord.fecha_corta)) / 86400000));
                    const sedeNombre = (sedes.find(s => s.id === ord.sede_id) || {}).nombre || '';
                    return (
                      <div key={ord.id} className="bg-[#111114] border border-[#222227] rounded-2xl p-4 space-y-3">
                        <div className="flex justify-between items-start gap-3">
                          <div className="min-w-0">
                            <div className="text-sm font-bold text-white truncate">{ord.cliente_nombre}</div>
                            <div className="text-[10px] text-zinc-500">
                              {ord.fecha_corta} {ord.created_at}
                              {sedeNombre ? ' · ' + sedeNombre : ''}
                              {dias > 0 ? ` · hace ${dias} día${dias > 1 ? 's' : ''}` : ' · hoy'}
                            </div>
                            {ord.cliente_telefono && (
                              <a href={`tel:${ord.cliente_telefono}`} className="text-[11px] text-[#60a5fa] underline">{ord.cliente_telefono}</a>
                            )}
                          </div>
                          <div className="text-right shrink-0">
                            <div className="font-mono font-black text-amber-400 text-lg">${ord.total_venta_usd.toFixed(2)}</div>
                            <div className="text-[10px] text-zinc-500 font-mono">Bs {(ord.total_venta_usd * ord.tasa_aplicada).toFixed(2)}</div>
                          </div>
                        </div>
                        <div className="text-[11px] text-zinc-400">
                          {ord.items.map(it => `${it.cantidad}x ${it.nombre}`).join(' · ')}
                        </div>
                        <AccionesOrden ord={ord} />
                        <button
                          onClick={() => cobrarFiado(ord)}
                          disabled={cobrandoId === ord.id}
                          className="w-full py-2.5 bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 text-white text-xs font-bold rounded-xl flex items-center justify-center space-x-2 transition"
                        >
                          <CheckCircle2 className="w-4 h-4" />
                          <span>{cobrandoId === ord.id ? 'Registrando…' : `Cobrar con ${metodoCobroFiado}`}</span>
                        </button>
                      </div>
                    );
                  })}

                {ordenes.filter(o => o.estado_pago === 'FIADO').length === 0 && (
                  <div className="py-12 text-center text-zinc-500 text-xs font-medium">No hay cuentas fiadas pendientes.</div>
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
                          {ord.referencia_pago && (
                            <div className="text-[11px] text-zinc-500 mt-1">Ref. de pago: <span className="font-mono text-zinc-300">{ord.referencia_pago}</span></div>
                          )}
                          <div className="mt-2"><AccionesOrden ord={ord} /></div>
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
        {vistaActual === 'admin' && usuarioActual.rol === 'superusuario' && (
          <AdminPanel
            sedes={sedes}
            insumos={insumos}
            usuarioActual={usuarioActual}
            onCambio={cargarDesdeBackend}
          />
        )}

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
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 text-xs">
                <div>
                  <label className="text-zinc-400 block mb-1">% de reserva operativa</label>
                  <input
                    type="number"
                    value={pctReservaOperativa}
                    onChange={e => setPctReservaOperativa(parseFloat(e.target.value) || 0)}
                    className="w-full bg-[#09090b] border border-[#222227] rounded-xl p-2.5 text-white font-mono font-bold"
                  />
                </div>
                <div>
                  <label className="text-zinc-400 block mb-1">Gastos fijos de la jornada (USD)</label>
                  <input
                    type="number"
                    value={gastosOperativosUSD}
                    onChange={e => setGastosOperativosUSD(parseFloat(e.target.value) || 0)}
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
                        pct_reserva_operativa: pctReservaOperativa,
                        gastos_operativos_fijos: gastosOperativosUSD
                      })
                    }).then(r => r.json().catch(() => ({}))).then(d => alert(d.success ? 'Tasas guardadas en la base de datos.' : ('No se pudo guardar: ' + (d.error || 'error')))).catch(() => alert('Sin conexión: no se guardó.'));
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
                    }).then(r => r.json().catch(() => ({}))).then(d => alert(d.success ? 'Datos de Pago Móvil y QR guardados correctamente.' : ('No se pudo guardar: ' + (d.error || 'error')))).catch(() => alert('Sin conexión: no se guardó.'));
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
          <span className="relative">
            <ChefHat className="w-5 h-5" />
            {pedidosWebActivos.length > 0 && (
              <span className="absolute -top-1.5 -right-2.5 min-w-[16px] h-4 px-1 rounded-full bg-red-500 text-white text-[9px] font-black flex items-center justify-center">
                {pedidosWebActivos.length}
              </span>
            )}
          </span>
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
            onClick={() => setVistaActual('admin')}
            className={`flex flex-col items-center transition ${
              vistaActual === 'admin' ? 'text-[#2563eb]' : 'text-zinc-500 hover:text-zinc-300'
            }`}
          >
            <DollarSign className="w-5 h-5" />
            <span className="text-[10px] font-medium mt-0.5">Admin</span>
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
                onClick={() => { setModalCobroAbierto(false); setEsFiado(false); }}
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

            <div className={`rounded-xl border p-3 space-y-2 text-xs ${esFiado ? 'bg-amber-950/30 border-amber-700' : 'bg-[#09090b] border-[#222227]'}`}>
              <label className="flex items-center space-x-2 cursor-pointer">
                <input type="checkbox" checked={esFiado} onChange={e => setEsFiado(e.target.checked)} className="w-4 h-4" />
                <span className="font-bold text-zinc-100">🧾 Venta fiada (el cliente paga después)</span>
              </label>
              {esFiado && (
                <div className="text-[11px] text-amber-300">
                  Se descuenta la mercancía ahora y la venta queda en <b>Cocina → Cuentas Fiadas</b> y en <b>Quién te debe</b> hasta que la cobres.
                </div>
              )}
              <input
                type="text"
                maxLength={80}
                value={clienteNombre}
                onChange={e => setClienteNombre(e.target.value)}
                placeholder={esFiado ? 'Nombre del cliente (obligatorio)' : 'Nombre del cliente (opcional)'}
                className="w-full bg-[#111114] border border-[#222227] rounded-xl p-2.5 text-white outline-none focus:border-[#2563eb]"
              />
              <input
                type="tel"
                inputMode="tel"
                maxLength={20}
                value={clienteTelefono}
                onChange={e => setClienteTelefono(e.target.value)}
                placeholder="Teléfono / WhatsApp (para enviarle la factura)"
                className="w-full bg-[#111114] border border-[#222227] rounded-xl p-2.5 text-white outline-none focus:border-[#2563eb]"
              />
            </div>

            {!esFiado && (<>
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

            <div className="bg-[#09090b] border border-[#222227] rounded-xl p-3 space-y-2 text-xs">
              <div className="font-bold text-zinc-300">Comprobante del pago (opcional)</div>
              <input
                type="text"
                maxLength={60}
                value={referenciaPago}
                onChange={e => setReferenciaPago(e.target.value)}
                placeholder="N° de referencia del pago móvil / punto"
                className="w-full bg-[#111114] border border-[#222227] rounded-xl p-2.5 font-mono text-white outline-none focus:border-[#2563eb]"
              />
              <label className="block w-full text-center py-2 bg-zinc-800 hover:bg-zinc-700 text-white font-bold rounded-xl cursor-pointer">
                <input type="file" accept="image/*" className="hidden"
                  onChange={e => setFotoComprobante((e.target.files && e.target.files[0]) || null)} />
                <span>{fotoComprobante ? '✓ ' + fotoComprobante.name : '📷 Tomar o adjuntar foto de la referencia'}</span>
              </label>
              {fotoComprobante && (
                <button type="button" onClick={() => setFotoComprobante(null)} className="text-[11px] text-zinc-400 underline">Quitar foto</button>
              )}
            </div>

            </>)}

            <button
              onClick={confirmarVenta}
              disabled={(!esFiado && diferenciaPago < -0.05) || (esFiado && clienteNombre.trim().length < 2)}
              className={`w-full py-3 disabled:opacity-40 disabled:cursor-not-allowed text-white font-bold text-sm rounded-xl transition ${esFiado ? 'bg-amber-600 hover:bg-amber-500' : 'bg-[#2563eb] hover:bg-[#1d4ed8]'}`}
            >
              {esFiado ? 'Registrar fiado y descontar receta' : 'Confirmar Cobro & Descontar Receta (BOM)'}
            </button>
          </div>
        </div>
      )}

      {ventaReciente && (
        <div className="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-center justify-center p-4 print:hidden">
          <div className="bg-[#111114] border border-[#222227] w-full max-w-sm rounded-2xl p-5 space-y-3">
            <div className="text-center space-y-1">
              <div className="text-3xl">✅</div>
              <h3 className="font-bold text-sm text-white">Venta #{ventaReciente.id} registrada</h3>
              <div className="font-mono font-black text-[#60a5fa]">Bs {ventaReciente.totalBs.toFixed(2)}</div>
              {ventaReciente.fiado && <div className="text-[11px] text-amber-400 font-bold">Pendiente de pago (fiado)</div>}
              {ventaReciente.comprobante === 'subiendo' && <div className="text-[11px] text-zinc-400">Subiendo comprobante a Google Drive…</div>}
              {ventaReciente.comprobante === 'ok' && <div className="text-[11px] text-emerald-400">Comprobante guardado en Drive ✓</div>}
              {ventaReciente.comprobante && ventaReciente.comprobante !== 'ok' && ventaReciente.comprobante !== 'subiendo' && (
                <div className="text-[11px] text-red-400">No se pudo guardar el comprobante: {ventaReciente.comprobante}</div>
              )}
            </div>
            <button
              onClick={() => abrirFactura(ventaReciente.id, true)}
              className="w-full py-3 bg-emerald-600 hover:bg-emerald-500 text-white font-bold text-sm rounded-xl transition"
            >
              📲 Enviar factura por WhatsApp
            </button>
            {!ventaReciente.telefono && (
              <div className="text-[10px] text-zinc-500 text-center">Sin teléfono del cliente: en WhatsApp elegirás el contacto.</div>
            )}
            <button onClick={() => abrirFactura(ventaReciente.id, false)} className={btnSec + ' w-full'}>🧾 Ver factura</button>
            <Comprobantes key={ventaReciente.comprobante || 'sin'} entidad="orden" entidadId={ventaReciente.id} />
            <button
              onClick={() => setVentaReciente(null)}
              className="w-full py-2.5 bg-[#2563eb] hover:bg-[#1d4ed8] text-white font-bold text-xs rounded-xl transition"
            >
              Listo
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

const fmtBsP = (n) => 'Bs. ' + (Number(n) || 0).toLocaleString('es-VE', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

function sonarAviso() {
  try {
    const C = window.AudioContext || window.webkitAudioContext;
    const ctx = new C();
    const o = ctx.createOscillator();
    const gn = ctx.createGain();
    o.connect(gn);
    gn.connect(ctx.destination);
    o.frequency.value = 880;
    gn.gain.value = 0.15;
    o.start();
    setTimeout(() => { o.stop(); ctx.close(); }, 350);
    if (navigator.vibrate) navigator.vibrate([200, 100, 200]);
  } catch (e) {}
}

function Cuenta({ segundos }) {
  const [hasta] = useState(() => Date.now() + (Number(segundos) || 0) * 1000);
  const [ahora, setAhora] = useState(Date.now());
  useEffect(() => {
    const t = setInterval(() => setAhora(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  const rest = Math.max(0, Math.round((hasta - ahora) / 1000));
  return (
    <span className="font-mono font-black">
      {String(Math.floor(rest / 60)).padStart(2, '0')}:{String(rest % 60).padStart(2, '0')}
    </span>
  );
}

const TIPOS_MENU = [['COMBO', 'Combos'], ['INDIVIDUAL', 'Para comer'], ['BEBIDA', 'Bebidas'], ['EXTRA', 'Extras']];

function TiendaPublica({ onIrLogin }) {
  const tokenInicial = (() => {
    const m = window.location.pathname.match(/^\/pedido\/([^/]+)/);
    if (m) return m[1];
    try { return localStorage.getItem('pedido_token') || ''; } catch (e) { return ''; }
  })();
  const datosGuardados = (() => {
    try { return JSON.parse(localStorage.getItem('pedido_datos') || '{}'); } catch (e) { return {}; }
  })();

  const [menu, setMenu] = useState(null);
  const [sedeId, setSedeId] = useState(null);
  const [cargandoMenu, setCargandoMenu] = useState(true);
  const [cantidades, setCantidades] = useState({});
  const [paso, setPaso] = useState(tokenInicial ? 'pedido' : 'menu');
  const [datos, setDatos] = useState({ nombre: datosGuardados.nombre || '', telefono: datosGuardados.telefono || '', notas: '' });
  const [token, setToken] = useState(tokenInicial);
  const [pedido, setPedido] = useState(null);
  const [error, setError] = useState('');
  const [enviando, setEnviando] = useState(false);
  const [pago, setPago] = useState({ metodo: 'Pago móvil', referencia: '', monto: '', foto: null });

  const cargarMenu = async (sede) => {
    try {
      const res = await fetch('/api/publico/menu' + (sede ? '?sede_id=' + sede : ''));
      const d = await res.json();
      if (d.success) { setMenu(d); setSedeId(d.sede_id); }
    } catch (e) {}
    setCargandoMenu(false);
  };

  const nuevoPedido = () => {
    try { localStorage.removeItem('pedido_token'); } catch (e) {}
    try { window.history.replaceState(null, '', '/'); } catch (e) {}
    setToken('');
    setPedido(null);
    setPaso('menu');
    setError('');
    cargarMenu(sedeId);
  };

  const cargarPedido = async () => {
    if (!token) return;
    try {
      const res = await fetch('/api/publico/pedido/' + token);
      const d = await res.json().catch(() => ({}));
      if (res.ok && d.success) setPedido(d.pedido);
      else if (res.status === 400 || res.status === 404) nuevoPedido();
    } catch (e) {}
  };

  useEffect(() => { cargarMenu(null); }, []);
  useEffect(() => {
    if (paso !== 'menu') return;
    const t = setInterval(() => cargarMenu(sedeId), 30000);
    return () => clearInterval(t);
  }, [paso, sedeId]);

  const estadoActual = pedido ? pedido.estado : null;
  useEffect(() => {
    if (!token) return;
    cargarPedido();
    if (['EXPIRADA', 'RECHAZADA', 'CONFIRMADA'].includes(estadoActual)) return;
    const t = setInterval(cargarPedido, 5000);
    return () => clearInterval(t);
  }, [token, estadoActual]);

  const productos = menu ? menu.productos : [];
  const cambiar = (p, delta) => {
    setCantidades(prev => {
      const actual = prev[p.id] || 0;
      const tope = p.disponible === null ? 20 : Math.min(20, p.disponible);
      return { ...prev, [p.id]: Math.max(0, Math.min(tope, actual + delta)) };
    });
  };
  const lineas = productos.filter(p => (cantidades[p.id] || 0) > 0);
  const unidades = lineas.reduce((a, p) => a + cantidades[p.id], 0);
  const totalBs = lineas.reduce((a, p) => a + p.precio_bs * cantidades[p.id], 0);

  const crearPedido = async () => {
    setError('');
    if (datos.nombre.trim().length < 2) { setError('Escribe tu nombre.'); return; }
    if (datos.telefono.replace(/\D/g, '').length < 10) { setError('Escribe un teléfono válido, por ejemplo 0412-1234567.'); return; }
    setEnviando(true);
    try {
      const res = await fetch('/api/publico/pedido', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          sede_id: sedeId, nombre: datos.nombre, telefono: datos.telefono, notas: datos.notas,
          items: lineas.map(p => ({ producto_id: p.id, cantidad: cantidades[p.id] }))
        })
      });
      const d = await res.json().catch(() => ({}));
      if (!res.ok || d.success === false) {
        setError(d.error || 'No se pudo crear el pedido.');
        cargarMenu(sedeId);
      } else {
        try {
          localStorage.setItem('pedido_token', d.token);
          localStorage.setItem('pedido_datos', JSON.stringify({ nombre: datos.nombre, telefono: datos.telefono }));
        } catch (e) {}
        try { window.history.replaceState(null, '', '/pedido/' + d.token); } catch (e) {}
        setPedido(null);
        setCantidades({});
        setToken(d.token);
        setPaso('pedido');
      }
    } catch (e) { setError('Sin conexión. Intenta de nuevo.'); }
    setEnviando(false);
  };

  const enviarPago = async () => {
    setError('');
    if ((pago.referencia || '').trim().length < 4 && !pago.foto) { setError('Escribe el número de referencia o adjunta la captura del pago.'); return; }
    setEnviando(true);
    try {
      const fd = new FormData();
      fd.append('metodo', pago.metodo);
      fd.append('referencia', pago.referencia || '');
      fd.append('monto_bs', pago.monto || (pedido ? pedido.total_bs : ''));
      if (pago.foto) fd.append('file', pago.foto);
      const res = await fetch('/api/publico/pedido/' + token + '/pago', { method: 'POST', body: fd });
      const d = await res.json().catch(() => ({}));
      if (!res.ok || d.success === false) setError(d.error || 'No se pudo registrar el pago.');
      else { setPago({ metodo: 'Pago móvil', referencia: '', monto: '', foto: null }); await cargarPedido(); }
    } catch (e) { setError('Sin conexión. Intenta de nuevo.'); }
    setEnviando(false);
  };

  const copiar = (t) => { try { navigator.clipboard.writeText(String(t)); } catch (e) {} };
  const inputP = 'w-full bg-[#09090b] border border-[#2a2a31] rounded-xl p-3 text-sm text-white outline-none focus:border-amber-500';

  const pie = (
    <div className="mt-10 pb-24 text-center space-y-2">
      <button onClick={onIrLogin} className="text-[11px] text-zinc-500 hover:text-zinc-300 underline underline-offset-4">
        Iniciar sesión
      </button>
      <div className="text-[10px] text-zinc-700">{menu ? menu.negocio : ''}</div>
    </div>
  );

  // ---------- Pantalla del pedido (cuenta regresiva, pago y estado) ----------
  if (paso === 'pedido') {
    return (
      <div className="min-h-screen bg-[#09090b] text-white">
        <div className="max-w-md mx-auto p-4 space-y-4">
          <div className="pt-2">
            <div className="text-[11px] uppercase tracking-widest text-amber-500 font-bold">{menu ? menu.negocio : 'Tu pedido'}</div>
            <h1 className="text-xl font-black">{pedido ? `Pedido #${pedido.id}` : 'Tu pedido'}</h1>
          </div>

          {!pedido && <div className="py-16 text-center text-zinc-500 text-sm">Cargando tu pedido…</div>}

          {pedido && (
            <>
              <div className="bg-[#111114] border border-[#222227] rounded-2xl p-4 space-y-2">
                {pedido.items.map((it, i) => (
                  <div key={i} className="flex justify-between text-sm">
                    <span className="text-zinc-200">{it.cantidad} × {it.nombre}</span>
                    <span className="font-mono text-zinc-300">{fmtBsP(it.total_bs)}</span>
                  </div>
                ))}
                <div className="flex justify-between items-baseline pt-2 border-t border-[#222227]">
                  <span className="text-xs text-zinc-400 uppercase font-bold">Total a pagar</span>
                  <span className="text-2xl font-black text-amber-400 font-mono">{fmtBsP(pedido.total_bs)}</span>
                </div>
                {pedido.sede && <div className="text-[11px] text-zinc-500">Retiro en: {pedido.sede}</div>}
              </div>

              {pedido.estado === 'PENDIENTE_PAGO' && (
                <>
                  <div className="bg-amber-950/40 border border-amber-800 rounded-2xl p-4 text-center">
                    <div className="text-xs text-amber-200">Tu pedido está apartado por</div>
                    <div className="text-4xl text-amber-400 my-1"><Cuenta key={pedido.id} segundos={pedido.segundos_restantes} /></div>
                    <div className="text-[11px] text-amber-200/80">Si no registras tu pago a tiempo, el pedido se cancela y la mercancía se libera.</div>
                  </div>

                  {pedido.pago && (
                    <div className="bg-[#111114] border border-[#222227] rounded-2xl p-4 space-y-2 text-sm">
                      <div className="text-[11px] uppercase font-bold text-zinc-400">Paga aquí</div>
                      <div className="text-zinc-200 font-bold">{pedido.pago.titulo}</div>
                      <div className="flex justify-between"><span className="text-zinc-400">Banco</span><span>{pedido.pago.banco}</span></div>
                      <div className="flex justify-between items-center"><span className="text-zinc-400">Teléfono</span>
                        <button onClick={() => copiar(pedido.pago.telefono)} className="font-mono underline decoration-dotted">{pedido.pago.telefono} 📋</button></div>
                      <div className="flex justify-between items-center"><span className="text-zinc-400">Cédula / RIF</span>
                        <button onClick={() => copiar(pedido.pago.rif_cedula)} className="font-mono underline decoration-dotted">{pedido.pago.rif_cedula} 📋</button></div>
                      <div className="flex justify-between items-center"><span className="text-zinc-400">Monto</span>
                        <button onClick={() => copiar(pedido.total_bs.toFixed(2))} className="font-mono font-bold text-amber-400 underline decoration-dotted">{fmtBsP(pedido.total_bs)} 📋</button></div>
                      {pedido.pago.qr_base64 && (
                        <img src={pedido.pago.qr_base64.startsWith('data:') ? pedido.pago.qr_base64 : 'data:image/png;base64,' + pedido.pago.qr_base64}
                          alt="QR de pago" className="mx-auto w-44 h-44 object-contain bg-white rounded-xl p-2" />
                      )}
                    </div>
                  )}

                  <div className="bg-[#111114] border border-[#222227] rounded-2xl p-4 space-y-3">
                    <div className="text-[11px] uppercase font-bold text-zinc-400">Ya pagué: registrar mi pago</div>
                    <select value={pago.metodo} onChange={e => setPago(p => ({ ...p, metodo: e.target.value }))} className={inputP}>
                      <option>Pago móvil</option><option>Transferencia</option><option>Zelle</option><option>Otro</option>
                    </select>
                    <input type="text" placeholder="Número de referencia" value={pago.referencia}
                      onChange={e => setPago(p => ({ ...p, referencia: e.target.value }))} className={inputP} />
                    <input type="number" inputMode="decimal" placeholder={`Monto pagado (Bs.) — ${pedido.total_bs.toFixed(2)}`} value={pago.monto}
                      onChange={e => setPago(p => ({ ...p, monto: e.target.value }))} className={inputP} />
                    <label className="block text-xs text-zinc-300 bg-[#09090b] border border-dashed border-[#2a2a31] rounded-xl p-3 text-center cursor-pointer">
                      {pago.foto ? `📎 ${pago.foto.name}` : '📷 Adjuntar captura o foto del comprobante'}
                      <input type="file" accept="image/*" className="hidden" onChange={e => setPago(p => ({ ...p, foto: e.target.files[0] || null }))} />
                    </label>
                    <button onClick={enviarPago} disabled={enviando} className="w-full py-3.5 rounded-xl bg-amber-500 hover:bg-amber-400 disabled:opacity-50 text-black font-black text-sm">
                      {enviando ? 'Enviando…' : 'Ya pagué, registrar pago'}
                    </button>
                  </div>
                </>
              )}

              {pedido.estado === 'PAGO_REPORTADO' && (
                <div className="bg-emerald-950/40 border border-emerald-800 rounded-2xl p-4 text-center space-y-1">
                  <div className="text-2xl">✅</div>
                  <div className="font-bold text-emerald-300">¡Recibimos tu pago!</div>
                  <div className="text-xs text-emerald-200/80">Lo estamos verificando. Tu pedido está apartado; esta pantalla se actualiza sola.</div>
                  {pedido.referencia && <div className="text-[11px] text-zinc-400">Referencia: {pedido.referencia}</div>}
                </div>
              )}

              {pedido.estado === 'CONFIRMADA' && (
                <div className="bg-emerald-950/40 border border-emerald-800 rounded-2xl p-4 text-center space-y-2">
                  <div className="text-2xl">🍔</div>
                  <div className="font-bold text-emerald-300">¡Pago confirmado! Tu pedido está en preparación.</div>
                  {pedido.factura_url && (
                    <a href={pedido.factura_url} target="_blank" rel="noreferrer" className="inline-block px-4 py-2 rounded-xl bg-[#18181b] border border-[#2a2a31] text-xs font-bold">
                      🧾 Ver mi factura
                    </a>
                  )}
                </div>
              )}

              {pedido.estado === 'EXPIRADA' && (
                <div className="bg-red-950/40 border border-red-900 rounded-2xl p-4 text-center space-y-2">
                  <div className="font-bold text-red-300">Se acabó el tiempo para pagar</div>
                  <div className="text-xs text-red-200/80">Liberamos la mercancía de este pedido. Si ya pagaste, escríbenos{pedido.contacto ? ` al ${pedido.contacto}` : ''} con tu comprobante.</div>
                </div>
              )}

              {pedido.estado === 'RECHAZADA' && (
                <div className="bg-red-950/40 border border-red-900 rounded-2xl p-4 text-center space-y-2">
                  <div className="font-bold text-red-300">No pudimos confirmar tu pago</div>
                  <div className="text-xs text-red-200/80">Tu pedido fue cancelado{pedido.contacto ? `. Escríbenos al ${pedido.contacto} si crees que es un error` : ''}.</div>
                </div>
              )}

              {error && <div className="text-xs font-bold text-red-400 text-center">{error}</div>}

              {['EXPIRADA', 'RECHAZADA', 'CONFIRMADA'].includes(pedido.estado) && (
                <button onClick={nuevoPedido} className="w-full py-3 rounded-xl bg-[#18181b] border border-[#2a2a31] text-sm font-bold">
                  Hacer otro pedido
                </button>
              )}
            </>
          )}
          {pie}
        </div>
      </div>
    );
  }

  // ---------- Datos del cliente ----------
  if (paso === 'datos') {
    return (
      <div className="min-h-screen bg-[#09090b] text-white">
        <div className="max-w-md mx-auto p-4 space-y-4">
          <button onClick={() => { setPaso('menu'); setError(''); }} className="text-xs text-zinc-400 pt-2">← Volver al menú</button>
          <h1 className="text-xl font-black">Confirma tu pedido</h1>
          <div className="bg-[#111114] border border-[#222227] rounded-2xl p-4 space-y-2">
            {lineas.map(p => (
              <div key={p.id} className="flex justify-between text-sm">
                <span className="text-zinc-200">{cantidades[p.id]} × {p.nombre}</span>
                <span className="font-mono text-zinc-300">{fmtBsP(p.precio_bs * cantidades[p.id])}</span>
              </div>
            ))}
            <div className="flex justify-between items-baseline pt-2 border-t border-[#222227]">
              <span className="text-xs text-zinc-400 uppercase font-bold">Total</span>
              <span className="text-2xl font-black text-amber-400 font-mono">{fmtBsP(totalBs)}</span>
            </div>
          </div>
          <div className="space-y-3">
            <input type="text" placeholder="Tu nombre" value={datos.nombre} onChange={e => setDatos(d => ({ ...d, nombre: e.target.value }))} className={inputP} />
            <input type="tel" inputMode="tel" placeholder="Tu teléfono (WhatsApp)" value={datos.telefono} onChange={e => setDatos(d => ({ ...d, telefono: e.target.value }))} className={inputP} />
            <input type="text" placeholder="Notas (opcional): sin cebolla, etc." value={datos.notas} onChange={e => setDatos(d => ({ ...d, notas: e.target.value }))} className={inputP} />
          </div>
          <div className="text-[11px] text-zinc-400 text-center">
            Apartamos tu pedido por {menu ? menu.vence_min : 15} minutos para que completes el pago. Si no lo registras a tiempo, se cancela.
          </div>
          {error && <div className="text-xs font-bold text-red-400 text-center">{error}</div>}
          <button onClick={crearPedido} disabled={enviando} className="w-full py-3.5 rounded-xl bg-amber-500 hover:bg-amber-400 disabled:opacity-50 text-black font-black text-sm">
            {enviando ? 'Apartando…' : 'Hacer pedido y pagar'}
          </button>
          {pie}
        </div>
      </div>
    );
  }

  // ---------- Menú ----------
  return (
    <div className="min-h-screen bg-[#09090b] text-white">
      <div className="max-w-md mx-auto p-4 space-y-5">
        <div className="pt-3 text-center">
          <h1 className="text-2xl font-black tracking-tight">{menu ? menu.negocio : 'Menú'}</h1>
          <div className="text-[11px] text-zinc-500 mt-1">Elige lo que quieres y paga desde tu teléfono</div>
        </div>

        {menu && menu.sedes.length > 1 && (
          <div className="flex gap-2 overflow-x-auto pb-1">
            {menu.sedes.map(sd => (
              <button key={sd.id} onClick={() => { setCantidades({}); setCargandoMenu(true); cargarMenu(sd.id); }}
                className={`px-3.5 py-1.5 rounded-full text-xs font-bold whitespace-nowrap border ${sd.id === sedeId ? 'bg-amber-500 text-black border-amber-500' : 'bg-[#111114] border-[#222227] text-zinc-300'}`}>
                {sd.nombre}
              </button>
            ))}
          </div>
        )}

        {cargandoMenu && <div className="py-16 text-center text-zinc-500 text-sm">Cargando el menú…</div>}
        {!cargandoMenu && menu && menu.productos.length === 0 && <div className="py-16 text-center text-zinc-500 text-sm">El menú no está disponible por ahora.</div>}

        {TIPOS_MENU.map(([tipo, titulo]) => {
          const lista = productos.filter(p => p.tipo === tipo);
          if (lista.length === 0) return null;
          return (
            <div key={tipo} className="space-y-2">
              <div className="text-[11px] uppercase tracking-widest text-amber-500 font-bold">{titulo}</div>
              {lista.map(p => {
                const agotado = p.disponible === 0;
                const cant = cantidades[p.id] || 0;
                return (
                  <div key={p.id} className={`bg-[#111114] border rounded-2xl p-3.5 flex items-center justify-between gap-3 ${agotado ? 'border-[#1c1c21] opacity-50' : 'border-[#222227]'}`}>
                    <div className="min-w-0">
                      <div className="font-bold text-sm">{p.nombre}</div>
                      <div className="mt-0.5">
                        <span className="font-mono font-black text-amber-400">{fmtBsP(p.precio_bs)}</span>
                        <span className="text-[10px] text-zinc-500 ml-2">${p.precio_usd.toFixed(2)}</span>
                      </div>
                      <div className={`text-[10px] mt-0.5 font-bold ${agotado ? 'text-red-400' : (p.disponible !== null && p.disponible <= 5) ? 'text-amber-400' : 'text-emerald-400'}`}>
                        {agotado ? 'Agotado' : p.disponible === null ? 'Disponible' : p.disponible >= 99 ? 'Disponibles: 99+' : `Disponibles: ${p.disponible}`}
                      </div>
                    </div>
                    {!agotado && (
                      <div className="flex items-center gap-2 shrink-0">
                        {cant > 0 && (
                          <>
                            <button onClick={() => cambiar(p, -1)} className="w-9 h-9 rounded-xl bg-[#18181b] border border-[#2a2a31] text-lg font-black">−</button>
                            <span className="w-5 text-center font-mono font-bold">{cant}</span>
                          </>
                        )}
                        <button onClick={() => cambiar(p, 1)} className="w-9 h-9 rounded-xl bg-amber-500 text-black text-lg font-black">+</button>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          );
        })}

        {pie}
      </div>

      {unidades > 0 && (
        <div className="fixed bottom-0 inset-x-0 p-3 bg-gradient-to-t from-[#09090b] via-[#09090b] to-transparent">
          <button onClick={() => { setError(''); setPaso('datos'); }} className="max-w-md mx-auto w-full flex items-center justify-between px-5 py-3.5 rounded-2xl bg-amber-500 hover:bg-amber-400 text-black font-black text-sm shadow-lg">
            <span>Ver pedido ({unidades})</span>
            <span className="font-mono">{fmtBsP(totalBs)}</span>
          </button>
        </div>
      )}
    </div>
  );
}

function PantallaLogin({ onOk, onVolver }) {
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
        {onVolver && (
          <button type="button" onClick={onVolver} className="w-full text-xs text-zinc-500 hover:text-zinc-300">
            ← Volver al menú
          </button>
        )}
      </form>
    </div>
  );
}

function App() {
  const [usuario, setUsuario] = useState(null);
  const [verificando, setVerificando] = useState(true);
  const [mostrarLogin, setMostrarLogin] = useState(false);

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
    setMostrarLogin(false);
    setUsuario(null);
  };

  if (verificando) {
    return <div className="min-h-screen bg-[#09090b] flex items-center justify-center text-zinc-400 text-sm">Cargando…</div>;
  }
  if (!usuario) {
    return mostrarLogin
      ? <PantallaLogin onOk={setUsuario} onVolver={() => setMostrarLogin(false)} />
      : <TiendaPublica onIrLogin={() => setMostrarLogin(true)} />;
  }
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
    if request.path.startswith("/api/") and "Cache-Control" not in resp.headers:
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/", methods=["GET"])
def index():
    return app.response_class(response=HTML_TEMPLATE, status=200, mimetype="text/html")


@app.route("/pedido/<token>", methods=["GET"])
def pagina_pedido(token):
    """El cliente vuelve a su pedido desde este enlace (la página lee el token de la dirección)."""
    resp = app.response_class(response=HTML_TEMPLATE, status=200, mimetype="text/html")
    resp.headers["X-Robots-Tag"] = "noindex"
    return resp

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    print(f">> Iniciando Burger Admin Unificado en http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
