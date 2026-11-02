"""
DOGGIPOS MULTISEDE & AUDITORÍA (FAST-FOOD) - VERSIÓN PRODUCCIÓN UNIFICADA
Monolito Flask + PostgreSQL (Supabase Cloud) / SQLite Fallback + SPA Completa
"""
import os
import sys
import json
import time
import sqlite3
import datetime
import requests
from flask import Flask, request, jsonify, render_template_string, session
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "doggipos-production-secret-key-2026")
app.config['JSON_SORT_KEYS'] = False

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

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "doggipos_local.db")

class DBWrapper:
    """Adaptador universal que ejecuta el mismo SQL en PostgreSQL (Supabase) y SQLite."""
    def __init__(self):
        self.is_pg = USE_POSTGRES
        if self.is_pg:
            self.conn = psycopg2.connect(DATABASE_URL)
        else:
            self.conn = sqlite3.connect(DB_PATH)
            self.conn.row_factory = sqlite3.Row

    def _adapt_sql(self, sql):
        if self.is_pg:
            sql = sql.replace("?", "%s")
            sql = sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
            sql = sql.replace("DATETIME('now')", "NOW()")
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
            pin_plain TEXT NOT NULL DEFAULT '1234',
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
        db.execute("INSERT INTO configuracion (tasa_bcv, tasa_manual, tasa_reposicion, usar_tasa_manual, pct_reserva_operativa, gastos_operativos_fijos) VALUES (?, ?, ?, ?, ?, ?)", (45.50, 46.00, 47.50, 0, 10.0, 0.0))

    row_sedes = db.execute("SELECT COUNT(*) as c FROM sedes").fetchone()
    if (row_sedes["c"] if isinstance(row_sedes, dict) else row_sedes[0]) == 0:
        db.execute("INSERT INTO sedes (nombre, es_almacen, activa) VALUES (?, ?, ?)", ('Almacén Central', 1, 1))
        db.execute("INSERT INTO sedes (nombre, es_almacen, activa) VALUES (?, ?, ?)", ('Sede 1 (Puesto Principal)', 0, 1))
        db.execute("INSERT INTO sedes (nombre, es_almacen, activa) VALUES (?, ?, ?)", ('Sede 2 (Puesto Norte)', 0, 1))

    row_users = db.execute("SELECT COUNT(*) as c FROM usuarios").fetchone()
    if (row_users["c"] if isinstance(row_users, dict) else row_users[0]) == 0:
        db.execute("INSERT INTO usuarios (nombre, username, pin_plain, pin_hash, rol, sede_id, activo) VALUES (?, ?, ?, ?, ?, ?, ?)",
                   ('Sahel Barrios (Superusuario)', 'superadmin', '1234', generate_password_hash("1234"), 'superusuario', None, 1))
        db.execute("INSERT INTO usuarios (nombre, username, pin_plain, pin_hash, rol, sede_id, activo) VALUES (?, ?, ?, ?, ?, ?, ?)",
                   ('Carlos Cajero', 'cajero1', '0001', generate_password_hash("0001"), 'cajero', 2, 1))
        db.execute("INSERT INTO usuarios (nombre, username, pin_plain, pin_hash, rol, sede_id, activo) VALUES (?, ?, ?, ?, ?, ?, ?)",
                   ('Ana Cajera', 'cajero2', '0002', generate_password_hash("0002"), 'cajero', 3, 1))

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
            db.execute("INSERT INTO catalogo_productos (nombre, tipo, precio_venta_usd, activo) VALUES (?, ?, ?, ?)", p)

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
                   (None, 'Pago Móvil Principal', 'Banesco (0134)', '0424-5371079', 'V-19000000', '', 1))

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

@app.route("/api/auth/login", methods=["POST"])
def api_login():
    data = request.json or {}
    pin = str(data.get("pin", "")).strip()
    user_id = data.get("user_id")
    db = get_db()
    users = [dict(u) for u in db.execute("SELECT * FROM usuarios WHERE activo = 1").fetchall()]
    db.close()

    matched = None
    for u in users:
        if user_id and int(u["id"]) != int(user_id):
            continue
        if u.get("pin_plain") == pin or u.get("pin_hash") == pin or check_password_hash(u.get("pin_hash", ""), pin):
            matched = u
            break

    if not matched:
        return jsonify({"success": False, "error": "PIN incorrecto o usuario inactivo."}), 401

    session["user_id"] = matched["id"]
    session["rol"] = matched["rol"]
    session["sede_id"] = matched["sede_id"]
    return jsonify({
        "success": True,
        "user": {
            "id": matched["id"],
            "nombre": matched["nombre"],
            "username": matched["username"],
            "rol": matched["rol"],
            "sede_id": matched["sede_id"]
        }
    })

@app.route("/api/init", methods=["GET"])
def api_init():
    db = get_db()
    cfg = dict(db.execute("SELECT * FROM configuracion WHERE id = 1").fetchone())
    sedes = [dict(s) for s in db.execute("SELECT * FROM sedes WHERE activa = 1 ORDER BY id ASC").fetchall()]
    usuarios = [dict(u) for u in db.execute("SELECT id, nombre, username, rol, sede_id, activo, pin_plain as pin FROM usuarios WHERE activo = 1 ORDER BY id ASC").fetchall()]
    insumos = [dict(i) for i in db.execute("SELECT * FROM insumos_maestro ORDER BY id ASC").fetchall()]
    productos = [dict(p) for p in db.execute("SELECT * FROM catalogo_productos WHERE activo = 1 ORDER BY id ASC").fetchall()]
    recetas = [dict(r) for r in db.execute("SELECT * FROM recetas_bom").fetchall()]
    inv_rows = [dict(r) for r in db.execute("SELECT sede_id, insumo_id, stock_actual FROM inventario_por_sede").fetchall()]
    qrs = [dict(q) for q in db.execute("SELECT * FROM metodos_pago_qr WHERE activo = 1 ORDER BY id ASC").fetchall()]
    ordenes_rows = [dict(o) for o in db.execute("SELECT * FROM ordenes ORDER BY id DESC LIMIT 200").fetchall()]
    items_rows = [dict(it) for it in db.execute("SELECT * FROM orden_items").fetchall()]
    logs_rows = [dict(l) for l in db.execute("SELECT * FROM auditoria_logs ORDER BY id DESC LIMIT 50").fetchall()]
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

    user_map = {u["id"]: u["nombre"] for u in usuarios}
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
def api_tasa_sync():
    data = request.json or {}
    db = get_db()
    if any(k in data for k in ["tasa_manual", "usar_tasa_manual", "tasa_reposicion", "pct_reserva_operativa", "gastos_operativos_fijos"]):
        t_man = float(data.get("tasa_manual", 46.0))
        u_man = 1 if data.get("usar_tasa_manual") else 0
        t_rep = float(data.get("tasa_reposicion", 47.5))
        pct_r = float(data.get("pct_reserva_operativa", 10.0))
        g_fij = float(data.get("gastos_operativos_fijos", 0.0))
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
def api_orden_crear():
    data = request.json or {}
    sede_id = int(data.get("sede_id", 2))
    cajero_id = int(data.get("cajero_id", 1))
    items = data.get("items", [])
    recibido_usd = float(data.get("recibido_usd", 0))
    recibido_bs = float(data.get("recibido_bs", 0))
    vuelto_usd = float(data.get("vuelto_usd", 0))
    vuelto_bs = float(data.get("vuelto_bs", 0))
    estado_pago = data.get("estado_pago", "PAGADA")
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
        items_para_insertar.append((p_id, cant, p_precio, costo_unit_prod, json.dumps(it.get("modificadores", []))))

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
        db.execute("UPDATE inventario_por_sede SET stock_actual = stock_actual - ? WHERE sede_id = ? AND insumo_id = ?", (cant_desc, sede_id, iid))
        db.execute("""
            INSERT INTO movimientos_kardex (sede_id, insumo_id, usuario_id, tipo, cantidad, costo_unitario_momento, motivo, created_at)
            VALUES (?, ?, ?, 'VENTA', ?, ?, ?, ?)
        """, (sede_id, iid, cajero_id, -cant_desc, insumos_map.get(iid, 0.0), f"Venta Orden #{orden_id}", ahora_str))

    db.commit()
    db.close()
    return jsonify({"success": True, "orden_id": orden_id})

@app.route("/api/orden/estado", methods=["PATCH"])
def api_orden_estado():
    data = request.json or {}
    orden_id = data.get("orden_id")
    estado_cocina = data.get("estado_cocina")
    estado_pago = data.get("estado_pago")
    db = get_db()
    if estado_cocina:
        db.execute("UPDATE ordenes SET estado_cocina = ? WHERE id = ?", (estado_cocina, orden_id))
    if estado_pago:
        db.execute("UPDATE ordenes SET estado_pago = ? WHERE id = ?", (estado_pago, orden_id))
    db.commit()
    db.close()
    return jsonify({"success": True})

@app.route("/api/auditoria/corregir", methods=["POST"])
def api_auditoria_corregir():
    data = request.json or {}
    orden_id = int(data.get("orden_id"))
    justificacion = str(data.get("justificacion", "")).strip()
    superusuario_id = int(data.get("superusuario_id", 1))

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
def api_inventario_movimiento():
    data = request.json or {}
    sede_id = int(data.get("sede_id", 2))
    insumo_id = int(data.get("insumo_id", 1))
    tipo = data.get("tipo", "MERMA")
    cantidad = float(data.get("cantidad", 1))
    motivo = data.get("motivo", "")
    sede_destino_id = data.get("sede_destino_id")
    usuario_id = int(data.get("usuario_id", 1))

    db = get_db()
    ins = db.execute("SELECT costo_unitario_usd FROM insumos_maestro WHERE id = ?", (insumo_id,)).fetchone()
    costo = float(ins["costo_unitario_usd"]) if ins else 0.0
    ahora_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if tipo in ["MERMA", "CONSUMO"]:
        db.execute("UPDATE inventario_por_sede SET stock_actual = stock_actual - ? WHERE sede_id = ? AND insumo_id = ?", (cantidad, sede_id, insumo_id))
        db.execute("INSERT INTO movimientos_kardex (sede_id, insumo_id, usuario_id, tipo, cantidad, costo_unitario_momento, motivo, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   (sede_id, insumo_id, usuario_id, tipo, -cantidad, costo, motivo, ahora_str))
    elif tipo == "COMPRA":
        db.execute("UPDATE inventario_por_sede SET stock_actual = stock_actual + ? WHERE sede_id = ? AND insumo_id = ?", (cantidad, sede_id, insumo_id))
        db.execute("INSERT INTO movimientos_kardex (sede_id, insumo_id, usuario_id, tipo, cantidad, costo_unitario_momento, motivo, created_at) VALUES (?, ?, ?, 'COMPRA', ?, ?, ?, ?)",
                   (sede_id, insumo_id, usuario_id, cantidad, costo, motivo, ahora_str))
    elif tipo == "TRANSFERENCIA" and sede_destino_id:
        sede_dest = int(sede_destino_id)
        db.execute("UPDATE inventario_por_sede SET stock_actual = stock_actual - ? WHERE sede_id = ? AND insumo_id = ?", (cantidad, sede_id, insumo_id))
        db.execute("UPDATE inventario_por_sede SET stock_actual = stock_actual + ? WHERE sede_id = ? AND insumo_id = ?", (cantidad, sede_dest, insumo_id))
        db.execute("INSERT INTO movimientos_kardex (sede_id, insumo_id, usuario_id, tipo, cantidad, costo_unitario_momento, motivo, created_at) VALUES (?, ?, ?, 'TRANSFERENCIA', ?, ?, ?, ?)",
                   (sede_id, insumo_id, usuario_id, -cantidad, costo, f"Envío a Sede {sede_dest}: {motivo}", ahora_str))

    db.commit()
    db.close()
    return jsonify({"success": True})

@app.route("/api/admin/crud", methods=["POST"])
def api_admin_crud():
    data = request.json or {}
    entidad = data.get("entidad")
    accion = data.get("accion")
    reg = data.get("registro", {})
    db = get_db()

    if entidad == "usuarios" and accion == "CREATE":
        pin_str = str(reg.get("pin", "0000")).strip()
        db.execute("INSERT INTO usuarios (nombre, username, pin_plain, pin_hash, rol, sede_id, activo) VALUES (?, ?, ?, ?, ?, ?, 1)",
                   (reg.get("nombre"), reg.get("username"), pin_str, generate_password_hash(pin_str), reg.get("rol", "cajero"), reg.get("sede_id")))
    elif entidad == "sedes" and accion == "CREATE":
        new_id = db.insert_returning_id("INSERT INTO sedes (nombre, es_almacen, activa) VALUES (?, 0, 1)", (reg.get("nombre"),))
        insumos = db.execute("SELECT id FROM insumos_maestro").fetchall()
        for i in insumos:
            db.execute("INSERT INTO inventario_por_sede (sede_id, insumo_id, stock_actual) VALUES (?, ?, 0)", (new_id, i["id"]))
    elif entidad == "metodos_qr" and accion == "UPDATE":
        db.execute("UPDATE metodos_pago_qr SET titulo = ?, banco = ?, telefono = ?, rif_cedula = ?, qr_base64 = ? WHERE id = 1",
                   (reg.get("titulo", "Pago Móvil"), reg.get("banco", ""), reg.get("telefono", ""), reg.get("rif_cedula", ""), reg.get("qr_base64", "")))
    elif entidad == "insumos" and accion == "UPDATE":
        cp = float(reg.get("costo_paquete_usd", 1))
        rp = float(reg.get("rendimiento_paquete", 1))
        cu = cp / rp if rp > 0 else 0
        db.execute("UPDATE insumos_maestro SET costo_paquete_usd = ?, rendimiento_paquete = ?, costo_unitario_usd = ? WHERE id = ?",
                   (cp, rp, cu, int(reg.get("id"))))
    elif entidad == "productos" and accion == "UPDATE":
        db.execute("UPDATE catalogo_productos SET precio_venta_usd = ? WHERE id = ?",
                   (float(reg.get("precio_venta_usd", 1.5)), int(reg.get("id"))))

    db.commit()
    db.close()
    return jsonify({"success": True})


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="es" class="dark">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no" />
  <title>DoggiPOS Multisede & Auditoría</title>
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
  <script type="text/babel" data-presets="react,typescript">
    const { useState, useEffect, useMemo, useRef } = React;

    // Generador ligero de iconos SVG vectoriales de alto contraste
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

// TIPOS DE DATOS DEL SISTEMA
// ==========================================

type RolUsuario = 'superusuario' | 'cajero';

interface Sede {
  id: number;
  nombre: string;
  es_almacen: boolean;
  activa: boolean;
}

interface Usuario {
  id: number;
  nombre: string;
  username: string;
  pin: string;
  rol: RolUsuario;
  sede_id: number | null;
  activo: boolean;
}

interface Insumo {
  id: number;
  nombre: string;
  unidad: string;
  costo_paquete_usd: number;
  rendimiento_paquete: number;
  costo_unitario_usd: number;
  alerta_stock_minimo: number;
}

interface RecetaItem {
  insumo_id: number;
  cantidad: number;
}

interface Producto {
  id: number;
  nombre: string;
  tipo: 'INDIVIDUAL' | 'COMBO' | 'BEBIDA' | 'EXTRA';
  precio_venta_usd: number;
  activo: boolean;
  receta: RecetaItem[];
}

interface OrdenItem {
  producto_id: number;
  nombre: string;
  cantidad: number;
  precio_unitario_usd: number;
  costo_unitario_usd: number;
  modificadores: string[];
}

interface Orden {
  id: number;
  turno_id?: number;
  sede_id: number;
  cajero_id: number;
  cajero_nombre: string;
  cliente_nombre?: string;
  cliente_telefono?: string;
  total_venta_usd: number;
  costo_insumos_usd: number;
  tasa_aplicada: number;
  tasa_reposicion_momento: number;
  recibido_usd: number;
  recibido_bs: number;
  vuelto_usd: number;
  vuelto_bs: number;
  metodo_qr_id?: number;
  referencia_pago?: string;
  estado_pago: 'PAGADA' | 'FIADO' | 'ANULADA';
  estado_cocina: 'PREPARANDO' | 'ENTREGADO';
  notas_cocina?: string;
  items: OrdenItem[];
  created_at: string; // ISO string o timestamp
  fecha_corta: string; // YYYY-MM-DD
}

interface AuditoriaLog {
  id: number;
  superusuario_id: number;
  sede_id: number;
  accion: 'ANULAR_ORDEN' | 'EDITAR_ORDEN' | 'AJUSTE_STOCK' | 'CAMBIO_PRECIO' | 'MERMA_DECLARADA';
  entidad_afectada: string;
  registro_id: number;
  snapshot_anterior: any;
  snapshot_nuevo: any;
  justificacion: string;
  created_at: string;
}

// ==========================================
// DATOS SEMILLA EXACTOS (SECCIÓN 4.3)
// ==========================================

const INSUMOS_SEMILLA: Insumo[] = [
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

const SEDES_SEMILLA: Sede[] = [
  { id: 1, nombre: 'Almacén Central', es_almacen: true, activa: true },
  { id: 2, nombre: 'Sede 1 (Puesto Plaza)', es_almacen: false, activa: true },
  { id: 3, nombre: 'Sede 2 (Puesto Boulevard)', es_almacen: false, activa: true },
];

const USUARIOS_SEMILLA: Usuario[] = [
  { id: 1, nombre: 'Propietario General', username: 'superadmin', pin: '1234', rol: 'superusuario', sede_id: null, activo: true },
  { id: 2, nombre: 'Carlos Cajero', username: 'cajero1', pin: '0001', rol: 'cajero', sede_id: 2, activo: true },
  { id: 3, nombre: 'Ana Cajera', username: 'cajero2', pin: '0002', rol: 'cajero', sede_id: 3, activo: true },
];

const PRODUCTOS_SEMILLA: Producto[] = [
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
function generarOrdenesHistoricas(): Orden[] {
  const hoy = new Date();
  const formatYMD = (d: Date) => d.toISOString().split('T')[0];

  const diasAtras = (dias: number) => {
    const d = new Date(hoy);
    d.setDate(d.getDate() - dias);
    return d;
  };

  const lista: Orden[] = [];
  let contador = 1000;

  // Distribución de órdenes en últimos 30 días
  const planes = [
    { dias: 0, sede: 2, cajero: 2, nombreCajero: 'Carlos Cajero', items: [{ prodId: 3, cant: 2 }, { prodId: 7, cant: 1 }], cliente: 'Pedro Perez', hora: '13:30' },
    { dias: 0, sede: 3, cajero: 3, nombreCajero: 'Ana Cajera', items: [{ prodId: 2, cant: 3 }, { prodId: 5, cant: 2 }], cliente: 'Maria Gomez', hora: '14:15' },
    { dias: 0, sede: 2, cajero: 2, nombreCajero: 'Carlos Cajero', items: [{ prodId: 4, cant: 1 }], cliente: 'Familia Lopez', hora: '16:00' },
    { dias: 1, sede: 2, cajero: 2, nombreCajero: 'Carlos Cajero', items: [{ prodId: 1, cant: 4 }, { prodId: 8, cant: 2 }], cliente: 'Andres Bello', hora: '18:20' },
    { dias: 1, sede: 3, cajero: 3, nombreCajero: 'Ana Cajera', items: [{ prodId: 3, cant: 3 }], cliente: 'Jose Rivas', hora: '19:40' },
    { dias: 2, sede: 2, cajero: 2, nombreCajero: 'Carlos Cajero', items: [{ prodId: 2, cant: 2 }, { prodId: 5, cant: 2 }], cliente: 'Laura Diaz', hora: '12:50' },
    { dias: 3, sede: 3, cajero: 3, nombreCajero: 'Ana Cajera', items: [{ prodId: 4, cant: 2 }], cliente: 'Combo Oficina', hora: '13:10' },
    { dias: 4, sede: 2, cajero: 2, nombreCajero: 'Carlos Cajero', items: [{ prodId: 1, cant: 6 }], cliente: 'Grupo Deportivo', hora: '20:15' },
    { dias: 5, sede: 3, cajero: 3, nombreCajero: 'Ana Cajera', items: [{ prodId: 3, cant: 4 }, { prodId: 6, cant: 1 }], cliente: 'Carolina Soto', hora: '15:30' },
    { dias: 7, sede: 2, cajero: 2, nombreCajero: 'Carlos Cajero', items: [{ prodId: 4, cant: 1 }, { prodId: 7, cant: 2 }], cliente: 'Roberto M.', hora: '17:00' },
    { dias: 10, sede: 3, cajero: 3, nombreCajero: 'Ana Cajera', items: [{ prodId: 2, cant: 4 }, { prodId: 5, cant: 4 }], cliente: 'Valeria C.', hora: '14:00' },
    { dias: 14, sede: 2, cajero: 2, nombreCajero: 'Carlos Cajero', items: [{ prodId: 3, cant: 5 }], cliente: 'Evento Infantil', hora: '16:45' },
    { dias: 20, sede: 3, cajero: 3, nombreCajero: 'Ana Cajera', items: [{ prodId: 4, cant: 3 }], cliente: 'Sindicato', hora: '19:10' },
    { dias: 25, sede: 2, cajero: 2, nombreCajero: 'Carlos Cajero', items: [{ prodId: 1, cant: 8 }, { prodId: 6, cant: 2 }], cliente: 'Fiesta Cumpleaños', hora: '21:00' },
  ];

  planes.forEach(p => {
    contador += 1;
    const fechaObj = diasAtras(p.dias);
    const fechaStr = formatYMD(fechaObj);

    let totalUSD = 0;
    let costoInsumos = 0;
    const ordenItems: OrdenItem[] = [];

    p.items.forEach(it => {
      const prod = PRODUCTOS_SEMILLA.find(x => x.id === it.prodId)!;
      const sub = prod.precio_venta_usd * it.cant;
      totalUSD += sub;

      // Calcular costo insumos
      let costoUnit = 0;
      prod.receta.forEach(r => {
        const ins = INSUMOS_SEMILLA.find(i => i.id === r.insumo_id)!;
        costoUnit += ins.costo_unitario_usd * r.cantidad;
      });
      costoInsumos += costoUnit * it.cant;

      ordenItems.push({
        producto_id: prod.id,
        nombre: prod.nombre,
        cantidad: it.cant,
        precio_unitario_usd: prod.precio_venta_usd,
        costo_unitario_usd: costoUnit,
        modificadores: []
      });
    });

    // Simulador de pago mixto: 70% pago en USD, 30% en Pago Móvil
    const esEfectivoPuro = contador % 2 === 0;
    const recUSD = esEfectivoPuro ? Math.ceil(totalUSD / 5) * 5 : totalUSD * 0.5;
    const vuelUSD = esEfectivoPuro ? recUSD - totalUSD : 0;
    const recBS = esEfectivoPuro ? 0 : (totalUSD * 0.5) * 45.50;

    lista.push({
      id: contador,
      sede_id: p.sede,
      cajero_id: p.cajero,
      cajero_nombre: p.nombreCajero,
      cliente_nombre: p.cliente,
      total_venta_usd: totalUSD,
      costo_insumos_usd: costoInsumos,
      tasa_aplicada: 45.50,
      tasa_reposicion_momento: 47.50,
      recibido_usd: recUSD,
      recibido_bs: recBS,
      vuelto_usd: vuelUSD,
      vuelto_bs: 0,
      estado_pago: 'PAGADA',
      estado_cocina: 'ENTREGADO',
      items: ordenItems,
      created_at: `${fechaStr} ${p.hora}`,
      fecha_corta: fechaStr
    });
  });

  return lista;
}

function App() {
  // 1. Estados base que vienen desde Supabase
  const [sedes, setSedes] = useState(SEDES_SEMILLA);
  const [usuariosSistema, setUsuariosSistema] = useState(USUARIOS_SEMILLA);
  const [metodosQR, setMetodosQR] = useState([{ id: 1, titulo: 'Pago Móvil Principal', banco: 'Banesco (0134)', telefono: '0424-5371079', rif_cedula: 'V-19000000', qr_base64: '' }]);
  const [motorBD, setMotorBD] = useState('Sincronizando...');
  const [mostrarQRModal, setMostrarQRModal] = useState(false);

  // 2. Estado de sesión y autenticación
  const [usuarioActual, setUsuarioActual] = useState(USUARIOS_SEMILLA[0]);
  const [sedeActivaId, setSedeActivaId] = useState(2); // Por defecto Sede 1
  const [vistaActual, setVistaActual] = useState('pos');
  const [tabCocina, setTabCocina] = useState('preparando');

  // 3. Configuración de Tasas y Finanzas
  const [tasaBCV, setTasaBCV] = useState(45.50);
  const [tasaManual, setTasaManual] = useState(46.00);
  const [tasaReposicion, setTasaReposicion] = useState(47.50);
  const [usarTasaManual, setUsarTasaManual] = useState(false);
  const [pctReservaOperativa, setPctReservaOperativa] = useState(10.0);
  const [gastosOperativosUSD, setGastosOperativosUSD] = useState(15.0);

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
  const [ordenes, setOrdenes] = useState(() => generarOrdenesHistoricas());
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
  const [carrito, setCarrito] = useState<{ producto: Producto; cantidad: number; modificadores: string[] }[]>([]);
  const [filtroCategoria, setFiltroCategoria] = useState<string>('TODOS');

  // Modales
  const [modalCobroAbierto, setModalCobroAbierto] = useState<boolean>(false);
  const [modalLoginAbierto, setModalLoginAbierto] = useState<boolean>(false);
  const [modalMermaAbierto, setModalMermaAbierto] = useState<boolean>(false);
  const [modalAuditoriaAbierto, setModalAuditoriaAbierto] = useState<boolean>(false);
  const [modalReportePrintAbierto, setModalReportePrintAbierto] = useState<boolean>(false);
  const [ordenParaAuditar, setOrdenParaAuditar] = useState<Orden | null>(null);
  const [justificacionAuditoria, setJustificacionAuditoria] = useState<string>('');

  // Variables de Cobro Inteligente
  const [cobroRecibidoUSD, setCobroRecibidoUSD] = useState<number>(0);
  const [cobroRecibidoBS, setCobroRecibidoBS] = useState<number>(0);
  const [cobroVueltoUSD, setCobroVueltoUSD] = useState<number>(0);
  const [cobroVueltoBS, setCobroVueltoBS] = useState<number>(0);
  const [clienteNombre, setClienteNombre] = useState<string>('');
  const [clienteTelefono, setClienteTelefono] = useState<string>('');
  const [esFiado, setEsFiado] = useState<boolean>(false);
  const [notasCocina, setNotasCocina] = useState<string>('');
  const [modificadorTemp, setModificadorTemp] = useState<string>('');
  const [copiado, setCopiado] = useState<string | null>(null);

  // ==========================================
  // ESTADO DEL MÓDULO DE REPORTES AVANZADOS
  // ==========================================
  const hoyStr = new Date().toISOString().split('T')[0];
  const hace7DiasStr = new Date(Date.now() - 7 * 86400000).toISOString().split('T')[0];

  const [reporteSubTab, setReporteSubTab] = useState<'ventas' | 'inventario' | 'rentabilidad' | 'cajeros'>('ventas');
  const [filtroPresetFecha, setFiltroPresetFecha] = useState<'hoy' | 'ayer' | '7dias' | '30dias' | 'personalizado'>('7dias');
  const [fechaInicioReporte, setFechaInicioReporte] = useState<string>(hace7DiasStr);
  const [fechaFinReporte, setFechaFinReporte] = useState<string>(hoyStr);
  const [filtroSedeReporte, setFiltroSedeReporte] = useState<number | 'todas'>('todas');

  const tasaActiva = usarTasaManual ? tasaManual : tasaBCV;
  const sedeActual = useMemo(() => sedes.find(s => s.id === sedeActivaId) || sedes[1], [sedes, sedeActivaId]);

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
  const calcularCostoBOM = (items: { producto: Producto; cantidad: number }[]) => {
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

  const agregarAlCarrito = (prod: Producto, mod: string = '') => {
    setCarrito(prev => {
      const idx = prev.findIndex(item => item.producto.id === prod.id && item.modificadores.join() === mod);
      if (idx > -1) {
        const copia = [...prev];
        copia[idx].cantidad += 1;
        return copia;
      }
      return [...prev, { producto: prod, cantidad: 1, modificadores: mod ? [mod] : [] }];
    });
    setModificadorTemp('');
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
    const nuevaOrden: Orden = {
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
    }).then(() => cargarDesdeBackend()).catch(err => console.warn('Guardado en cola local:', err));
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

    const log: AuditoriaLog = {
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
    const porDia: Record<string, { fecha: string; totalUSD: number; ordenes: number }> = {};
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
    const consumosPorInsumo: Record<number, number> = {};

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

      let velocidad: 'ALTA' | 'MEDIA' | 'ESTABLE' = 'ESTABLE';
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
    const statsProd: Record<number, { producto: Producto; cant: number; ingresosUSD: number; costoBOM: number }> = {};

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
    const cajerosList = USUARIOS_SEMILLA.filter(u => u.rol === 'cajero' || u.id === 1);

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
    link.setAttribute('download', `DoggiPOS_Reporte_${reporteSubTab}_${fechaInicioReporte}_a_${fechaFinReporte}.csv`);
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
      <header className="sticky top-0 z-40 bg-[#09090b]/95 backdrop-blur border-b border-[#27272a] px-4 py-3 print:hidden">
        <div className="max-w-6xl mx-auto flex items-center justify-between">
          <div className="flex items-center space-x-3">
            <div className="w-9 h-9 rounded-xl bg-[#2563eb] flex items-center justify-center font-black text-white text-xl tracking-tighter shadow-lg shadow-blue-900/30">
              D
            </div>
            <div>
              <div className="flex items-center space-x-2">
                <span className="font-extrabold text-sm tracking-tight text-white">DoggiPOS Multisede</span>
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
            <div className="bg-[#18181b] border border-[#27272a] px-3 py-1.5 rounded-xl text-right flex items-center space-x-2">
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
              className="bg-[#18181b] border border-[#27272a] hover:border-[#3b82f6] px-2.5 py-1.5 rounded-xl flex items-center space-x-2 transition"
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
                <div className="bg-[#18181b] border border-[#27272a] p-3 rounded-2xl flex items-center justify-between">
                  <div className="text-xs text-zinc-400 flex items-center space-x-1.5">
                    <Store className="w-4 h-4 text-[#2563eb]" />
                    <span className="font-semibold text-white">Sede POS:</span>
                  </div>
                  <select
                    value={sedeActivaId}
                    onChange={e => setSedeActivaId(Number(e.target.value))}
                    className="bg-[#09090b] border border-[#27272a] text-xs font-bold text-white rounded-xl px-3 py-1.5 focus:border-[#2563eb] outline-none"
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
                <div className="bg-[#18181b] border border-[#27272a] p-3 rounded-2xl flex items-center space-x-2 text-xs">
                  <Lock className="w-4 h-4 text-amber-500" />
                  <span className="text-zinc-400">Puesto Bloqueado a:</span>
                  <span className="font-bold text-white">{sedeActual.nombre}</span>
                </div>
              )}

              <div className="bg-[#18181b] border border-[#27272a] p-3 rounded-2xl flex items-center justify-between">
                <div className="text-xs">
                  <span className="text-zinc-400 text-[10px] uppercase font-bold block">Gaveta Física USD</span>
                  <span className="text-base font-black text-emerald-400 font-mono">
                    ${ordenes.filter(o => o.sede_id === sedeActivaId && o.estado_pago === 'PAGADA').reduce((acc, o) => acc + o.recibido_usd - o.vuelto_usd, 0).toFixed(2)}
                  </span>
                </div>
                <div className="text-[10px] text-zinc-500 text-right">Efectivo en billetes</div>
              </div>

              <div className="bg-[#18181b] border border-[#27272a] p-3 rounded-2xl flex items-center justify-between">
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
                      : 'bg-[#18181b] text-zinc-400 border-[#27272a] hover:text-white'
                  }`}
                >
                  {cat}
                </button>
              ))}
            </div>

            {/* Modificador rápido */}
            <div className="flex items-center space-x-2 text-xs bg-[#18181b] border border-[#27272a] p-2.5 rounded-2xl">
              <span className="text-zinc-400 text-[11px] font-semibold">Modificador rápido:</span>
              {['Sin cebolla', 'Sin mostaza', 'Extra salsa', 'Para llevar'].map(mod => (
                <button
                  key={mod}
                  onClick={() => setModificadorTemp(modificadorTemp === mod ? '' : mod)}
                  className={`px-2.5 py-1 rounded-lg border text-[11px] font-bold transition ${
                    modificadorTemp === mod
                      ? 'bg-amber-500 text-black border-amber-400'
                      : 'bg-[#09090b] text-zinc-300 border-[#27272a] hover:border-zinc-500'
                  }`}
                >
                  {mod}
                </button>
              ))}
            </div>

            {/* Grid de Productos Táctiles */}
            <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-3">
              {productos
                .filter(p => p.activo && (filtroCategoria === 'TODOS' || p.tipo === filtroCategoria))
                .map(prod => {
                  const bsPrice = prod.precio_venta_usd * tasaActiva;
                  return (
                    <div
                      key={prod.id}
                      onClick={() => agregarAlCarrito(prod, modificadorTemp)}
                      className="bg-[#18181b] border border-[#27272a] hover:border-[#2563eb] active:scale-98 transition p-3.5 rounded-2xl cursor-pointer flex flex-col justify-between group"
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

                      <div className="mt-4 pt-2 border-t border-[#27272a]/60 flex items-baseline justify-between">
                        <span className="text-base font-black text-white font-mono">
                          ${prod.precio_venta_usd.toFixed(2)}
                        </span>
                        <span className="text-xs font-mono font-bold text-[#60a5fa]">
                          Bs {bsPrice.toFixed(2)}
                        </span>
                      </div>
                    </div>
                  );
                })}
            </div>
          </div>
        )}

        {/* VISTA 2: COCINA Y FIADOS */}
        {vistaActual === 'cocina' && (
          <div className="space-y-4">
            <div className="flex border-b border-[#27272a] text-sm font-bold">
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
                      className="bg-[#18181b] border border-[#27272a] rounded-2xl p-4 flex flex-col justify-between space-y-3"
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

                      <div className="bg-[#09090b] p-3 rounded-xl border border-[#27272a] space-y-1.5 text-xs">
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
                  Control de Stock por Sede ({sedeActual.nombre})
                </h2>
                <p className="text-xs text-zinc-400 mt-0.5">
                  Descuento automático por receta (BOM). Semáforo de reposición inmediata.
                </p>
              </div>

              <div className="flex items-center space-x-2">
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
                const key = `${sedeActivaId}_${insumo.id}`;
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
                        : 'bg-[#18181b] border-[#27272a]'
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
                  <div className="bg-[#18181b] border border-[#27272a] p-4 rounded-2xl">
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

                  <div className="bg-[#18181b] border border-[#27272a] p-4 rounded-2xl">
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

                  <div className="bg-[#18181b] border border-[#27272a] p-4 rounded-2xl">
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
                <div className="bg-[#18181b] border border-[#27272a] rounded-2xl p-4 space-y-3">
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
                            : 'bg-[#09090b] border-[#27272a]'
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
                  Debes ingresar con el perfil o PIN de Superusuario (1234) para generar reportes financieros.
                </p>
                <button
                  onClick={() => setModalLoginAbierto(true)}
                  className="bg-[#2563eb] text-white text-xs font-bold px-4 py-2 rounded-xl mt-2"
                >
                  Cambiar a Superusuario
                </button>
              </div>
            ) : (
              <div className="space-y-4">
                {/* Cabecera del Módulo de Reportes con Exportación */}
                <div className="flex flex-col md:flex-row md:items-center justify-between gap-3 pb-2 border-b border-[#27272a]">
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
                      className="bg-[#18181b] hover:bg-zinc-800 border border-[#27272a] hover:border-emerald-500 text-white px-3.5 py-2 rounded-xl text-xs font-bold flex items-center space-x-2 transition shadow-sm"
                      title="Descargar datos en formato CSV con UTF-8 BOM"
                    >
                      <FileSpreadsheet className="w-4 h-4 text-emerald-400" />
                      <span>Exportar CSV</span>
                    </button>

                    <button
                      onClick={() => setModalReportePrintAbierto(true)}
                      className="bg-[#2563eb] hover:bg-[#1d4ed8] text-white px-3.5 py-2 rounded-xl text-xs font-bold flex items-center space-x-2 transition shadow-lg shadow-blue-900/30"
                      title="Ver vista de impresión o exportar a PDF"
                    >
                      <Printer className="w-4 h-4" />
                      <span>Generar PDF / Imprimir</span>
                    </button>
                  </div>
                </div>

                {/* Barra de Filtros: Rangos de Fecha Personalizables y Selector de Sede */}
                <div className="bg-[#18181b] border border-[#27272a] p-3.5 rounded-2xl flex flex-col lg:flex-row lg:items-center justify-between gap-3 text-xs">
                  {/* Selector de Presets de Fecha */}
                  <div className="flex items-center space-x-1.5 overflow-x-auto no-scrollbar pb-1 lg:pb-0">
                    <span className="text-zinc-400 text-[11px] font-bold mr-1 flex items-center">
                      <Calendar className="w-3.5 h-3.5 mr-1 text-[#2563eb]" /> Período:
                    </span>
                    {(['hoy', 'ayer', '7dias', '30dias', 'personalizado'] as const).map(preset => (
                      <button
                        key={preset}
                        onClick={() => setFiltroPresetFecha(preset)}
                        className={`px-3 py-1.5 rounded-xl border text-[11px] font-bold transition whitespace-nowrap ${
                          filtroPresetFecha === preset
                            ? 'bg-[#2563eb] text-white border-[#2563eb]'
                            : 'bg-[#09090b] text-zinc-400 border-[#27272a] hover:text-white'
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
                  <div className="flex flex-wrap items-center gap-2 pt-2 lg:pt-0 border-t lg:border-t-0 border-[#27272a]">
                    <div className="flex items-center space-x-1.5 bg-[#09090b] border border-[#27272a] px-2.5 py-1.5 rounded-xl">
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

                    <div className="flex items-center space-x-1.5 bg-[#09090b] border border-[#27272a] px-2.5 py-1.5 rounded-xl">
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
                    <div className="flex items-center space-x-1.5 bg-[#09090b] border border-[#27272a] px-2.5 py-1.5 rounded-xl">
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
                        onClick={() => setReporteSubTab(tab.id as any)}
                        className={`p-3 rounded-2xl border text-left flex items-center space-x-2.5 transition ${
                          activo
                            ? 'bg-[#2563eb]/20 border-[#2563eb] text-white shadow-md'
                            : 'bg-[#18181b] border-[#27272a] text-zinc-400 hover:text-white hover:border-zinc-700'
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
                      <div className="bg-[#18181b] border border-[#27272a] p-4 rounded-2xl">
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

                      <div className="bg-[#18181b] border border-[#27272a] p-4 rounded-2xl">
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

                      <div className="bg-[#18181b] border border-[#27272a] p-4 rounded-2xl">
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

                      <div className="bg-[#18181b] border border-[#27272a] p-4 rounded-2xl">
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
                    <div className="bg-[#18181b] border border-[#27272a] p-4 rounded-2xl space-y-3">
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
                                <div className="flex-1 bg-[#09090b] h-6 rounded-lg overflow-hidden border border-[#27272a] relative">
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
                    <div className="bg-[#18181b] border border-[#27272a] rounded-2xl overflow-hidden">
                      <div className="p-3.5 border-b border-[#27272a] flex justify-between items-center">
                        <h4 className="text-xs font-bold text-white uppercase tracking-wider">
                          Registro de Transacciones Filtradas ({reporte1_Ventas.ordenes.length})
                        </h4>
                      </div>
                      <div className="overflow-x-auto">
                        <table className="w-full text-left text-xs">
                          <thead className="bg-[#09090b] text-zinc-400 uppercase text-[10px] font-bold border-b border-[#27272a]">
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
                      <div className="bg-[#18181b] border border-[#27272a] p-4 rounded-2xl">
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

                      <div className="bg-[#18181b] border border-[#27272a] p-4 rounded-2xl">
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

                      <div className="bg-[#18181b] border border-[#27272a] p-4 rounded-2xl">
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
                    <div className="bg-[#18181b] border border-[#27272a] rounded-2xl overflow-hidden">
                      <div className="p-3.5 border-b border-[#27272a] flex justify-between items-center">
                        <h4 className="text-xs font-bold text-white uppercase tracking-wider">
                          Análisis de Insumos del BOM Maestro ({reporte2_Inventario.tablaInsumos.length})
                        </h4>
                      </div>
                      <div className="overflow-x-auto">
                        <table className="w-full text-left text-xs">
                          <thead className="bg-[#09090b] text-zinc-400 uppercase text-[10px] font-bold border-b border-[#27272a]">
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
                      <div className="bg-[#18181b] border border-[#27272a] p-4 rounded-2xl">
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

                      <div className="bg-[#18181b] border border-[#27272a] p-4 rounded-2xl">
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

                      <div className="bg-[#18181b] border border-[#27272a] p-4 rounded-2xl">
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
                    <div className="bg-[#18181b] border border-[#27272a] p-4 rounded-2xl space-y-3">
                      <h4 className="text-xs font-bold text-white uppercase tracking-wider">
                        Comparativa de Desempeño por Ubicación
                      </h4>
                      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                        {reporte3_Rentabilidad.sedesAnalizadas.map(sedeStat => (
                          <div
                            key={sedeStat.sede_id}
                            className="bg-[#09090b] border border-[#27272a] p-3.5 rounded-xl space-y-2"
                          >
                            <div className="flex justify-between items-center">
                              <span className="font-extrabold text-white text-xs">{sedeStat.nombre}</span>
                              <span className="text-[10px] bg-blue-950 text-blue-400 border border-blue-800 px-2 py-0.5 rounded font-bold">
                                {sedeStat.ordenes} órdenes
                              </span>
                            </div>
                            <div className="grid grid-cols-3 gap-2 pt-1 text-center">
                              <div className="bg-[#18181b] p-2 rounded-lg">
                                <span className="text-[9px] text-zinc-400 uppercase font-bold block">Ventas</span>
                                <span className="font-mono font-bold text-white text-xs">
                                  ${sedeStat.totalUSD.toFixed(2)}
                                </span>
                              </div>
                              <div className="bg-[#18181b] p-2 rounded-lg">
                                <span className="text-[9px] text-zinc-400 uppercase font-bold block">Costo BOM</span>
                                <span className="font-mono font-bold text-blue-400 text-xs">
                                  ${sedeStat.costoBOM.toFixed(2)}
                                </span>
                              </div>
                              <div className="bg-[#18181b] p-2 rounded-lg">
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
                    <div className="bg-[#18181b] border border-[#27272a] rounded-2xl overflow-hidden">
                      <div className="p-3.5 border-b border-[#27272a]">
                        <h4 className="text-xs font-bold text-white uppercase tracking-wider">
                          Rentabilidad y Margen por Producto
                        </h4>
                      </div>
                      <div className="overflow-x-auto">
                        <table className="w-full text-left text-xs">
                          <thead className="bg-[#09090b] text-zinc-400 uppercase text-[10px] font-bold border-b border-[#27272a]">
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
                          className="bg-[#18181b] border border-[#27272a] p-4 rounded-2xl space-y-3"
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
                            <div className="bg-[#09090b] p-2.5 rounded-xl border border-[#27272a]">
                              <span className="text-[9px] text-zinc-400 uppercase font-bold block">Órdenes</span>
                              <span className="font-mono font-black text-white text-base">
                                {cajero.ordenesProcesadas}
                              </span>
                            </div>
                            <div className="bg-[#09090b] p-2.5 rounded-xl border border-[#27272a]">
                              <span className="text-[9px] text-zinc-400 uppercase font-bold block">Facturado</span>
                              <span className="font-mono font-black text-emerald-400 text-base">
                                ${cajero.totalUSD.toFixed(2)}
                              </span>
                            </div>
                          </div>

                          <div className="pt-2 border-t border-[#27272a] flex justify-between items-center text-xs">
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
            <div className="bg-[#18181b] border border-[#27272a] p-4 rounded-2xl space-y-3">
              <h3 className="text-xs font-bold text-white uppercase">Motores de Tasas de Cambio</h3>
              <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 text-xs">
                <div>
                  <label className="text-zinc-400 block mb-1">Tasa Oficial BCV</label>
                  <input
                    type="number"
                    value={tasaBCV}
                    onChange={e => setTasaBCV(parseFloat(e.target.value) || 0)}
                    className="w-full bg-[#09090b] border border-[#27272a] rounded-xl p-2.5 text-white font-mono font-bold"
                  />
                </div>
                <div>
                  <label className="text-zinc-400 block mb-1">Tasa Manual POS</label>
                  <input
                    type="number"
                    value={tasaManual}
                    onChange={e => setTasaManual(parseFloat(e.target.value) || 0)}
                    className="w-full bg-[#09090b] border border-[#27272a] rounded-xl p-2.5 text-white font-mono font-bold"
                  />
                </div>
                <div>
                  <label className="text-zinc-400 block mb-1">Tasa de Reposición (Poder Real)</label>
                  <input
                    type="number"
                    value={tasaReposicion}
                    onChange={e => setTasaReposicion(parseFloat(e.target.value) || 0)}
                    className="w-full bg-[#09090b] border border-[#27272a] rounded-xl p-2.5 text-white font-mono font-bold"
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
            <div className="bg-[#18181b] border border-[#27272a] p-4 rounded-2xl space-y-3 text-xs">
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
                    className="w-full bg-[#09090b] border border-[#27272a] rounded-xl p-2.5 text-white font-bold"
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
                    className="w-full bg-[#09090b] border border-[#27272a] rounded-xl p-2.5 text-white font-mono font-bold"
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
                    className="w-full bg-[#09090b] border border-[#27272a] rounded-xl p-2.5 text-white font-mono font-bold"
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
                        const base64 = ev.target?.result as string;
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
            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 bg-[#18181b] border border-blue-900/50 p-4 rounded-2xl">
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
                href="/doggipos_deploy.zip"
                download="doggipos_deploy.zip"
                className="bg-[#2563eb] hover:bg-[#1d4ed8] text-white px-4 py-2.5 rounded-xl text-xs font-bold flex items-center justify-center space-x-2 shadow-lg shadow-blue-900/30 transition whitespace-nowrap"
              >
                <Download className="w-4 h-4" />
                <span>Descargar doggipos_deploy.zip</span>
              </a>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
              <div className="bg-[#18181b] border border-[#27272a] p-4 rounded-2xl">
                <div className="flex items-center space-x-2 text-white font-bold text-xs mb-2">
                  <FileCode className="w-4 h-4 text-blue-400" />
                  <span>render.yaml</span>
                </div>
                <div className="text-[10px] bg-emerald-950/60 border border-emerald-800 text-emerald-400 px-2 py-1 rounded font-bold inline-block">
                  ✓ Configurado en la raíz
                </div>
              </div>

              <div className="bg-[#18181b] border border-[#27272a] p-4 rounded-2xl">
                <div className="flex items-center space-x-2 text-white font-bold text-xs mb-2">
                  <FileCode className="w-4 h-4 text-amber-400" />
                  <span>requirements.txt</span>
                </div>
                <div className="text-[10px] bg-emerald-950/60 border border-emerald-800 text-emerald-400 px-2 py-1 rounded font-bold inline-block">
                  ✓ Configurado en la raíz
                </div>
              </div>

              <div className="bg-[#18181b] border border-[#27272a] p-4 rounded-2xl">
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
          <div className="bg-[#18181b]/95 backdrop-blur border border-[#27272a] rounded-2xl p-3.5 shadow-2xl flex items-center justify-between">
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
                className="bg-[#2563eb] hover:bg-[#1d4ed8] disabled:opacity-40 disabled:cursor-not-allowed text-white text-xs font-bold px-5 py-3 rounded-xl transition flex items-center space-x-2 shadow-lg shadow-blue-900/30"
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
      <nav className="fixed bottom-0 left-0 right-0 z-40 bg-[#09090b] border-t border-[#27272a] max-w-6xl mx-auto flex items-center justify-around py-2 print:hidden">
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
          <div className="bg-white text-zinc-900 w-full max-w-4xl rounded-2xl p-6 sm:p-8 space-y-6 shadow-2xl my-auto print:p-0 print:shadow-none print:w-full">
            {/* Cabecera del Documento Imprimible */}
            <div className="flex justify-between items-start border-b-2 border-zinc-900 pb-4">
              <div>
                <div className="flex items-center space-x-2">
                  <div className="w-8 h-8 rounded-lg bg-blue-600 flex items-center justify-center text-white font-black text-lg">
                    D
                  </div>
                  <h1 className="text-xl font-black tracking-tight text-zinc-950 uppercase">
                    DoggiPOS Multisede & Auditoría
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
          <div className="bg-[#18181b] border border-[#27272a] w-full max-w-lg rounded-t-3xl sm:rounded-2xl p-5 space-y-4 max-h-[90vh] overflow-y-auto">
            <div className="flex justify-between items-center pb-2 border-b border-[#27272a]">
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

            <div className="bg-[#09090b] p-3 rounded-xl border border-[#27272a] flex justify-between items-center">
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
                    className="w-full bg-[#09090b] border border-[#27272a] rounded-xl p-2.5 font-mono font-bold text-white focus:border-[#2563eb] outline-none"
                  />
                </div>
                <div>
                  <label className="block text-zinc-400 mb-1">Pago Móvil / Punto (Bs)</label>
                  <input
                    type="number"
                    step="1"
                    value={cobroRecibidoBS}
                    onChange={e => setCobroRecibidoBS(parseFloat(e.target.value) || 0)}
                    className="w-full bg-[#09090b] border border-[#27272a] rounded-xl p-2.5 font-mono font-bold text-white focus:border-[#2563eb] outline-none"
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
                    className="w-full bg-[#09090b] border border-[#27272a] rounded-xl p-2.5 font-mono font-bold text-white focus:border-[#2563eb] outline-none"
                  />
                </div>
                <div>
                  <label className="block text-zinc-400 mb-1">Vuelto Cruzado (Bs Móvil)</label>
                  <input
                    type="number"
                    step="1"
                    value={cobroVueltoBS}
                    onChange={e => setCobroVueltoBS(parseFloat(e.target.value) || 0)}
                    className="w-full bg-[#09090b] border border-[#27272a] rounded-xl p-2.5 font-mono font-bold text-white focus:border-[#2563eb] outline-none"
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
            <div className="bg-[#09090b] border border-[#27272a] rounded-xl p-3 space-y-2 text-xs">
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
              <div className="font-mono text-[11px] text-zinc-300 bg-[#18181b] p-2 rounded-lg border border-[#27272a] flex justify-between items-center">
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
                  const msg = `🌭 *Pedido DoggiPOS*\nTotal a pagar: *$${totalCarritoUSD.toFixed(2)}* (*Bs ${totalCarritoBS.toFixed(2)}*)\n\n📲 *Datos Pago Móvil:*\nBanco: ${metodosQR[0]?.banco}\nTeléfono: ${metodosQR[0]?.telefono}\nCédula/RIF: ${metodosQR[0]?.rif_cedula}`;
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
          <div className="bg-[#18181b] border border-[#27272a] w-full max-w-sm rounded-2xl p-5 space-y-4">
            <div className="flex justify-between items-center pb-2 border-b border-[#27272a]">
              <h3 className="font-bold text-sm text-white">Seleccionar Usuario & PIN</h3>
              <button onClick={() => setModalLoginAbierto(false)} className="text-zinc-400 hover:text-white">
                ✕
              </button>
            </div>

            <div className="space-y-2">
              {USUARIOS_SEMILLA.map(u => (
                <button
                  key={u.id}
                  onClick={() => {
                    setUsuarioActual(u);
                    setModalLoginAbierto(false);
                  }}
                  className={`w-full p-3 rounded-xl border text-left transition flex items-center justify-between ${
                    usuarioActual.id === u.id
                      ? 'bg-[#2563eb]/20 border-[#2563eb] text-white'
                      : 'bg-[#09090b] border-[#27272a] text-zinc-300 hover:border-zinc-500'
                  }`}
                >
                  <div>
                    <div className="font-bold text-xs">{u.nombre}</div>
                    <div className="text-[10px] text-zinc-400 uppercase font-semibold">
                      {u.rol} • {u.sede_id ? `Sede ${u.sede_id}` : 'Acceso Global'}
                    </div>
                  </div>
                  <span className="font-mono text-xs text-zinc-400">PIN: {u.pin}</span>
                </button>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* ============================================================== */}
      {/* MODAL DE AUDITORÍA: ANULAR ORDEN                               */}
      {/* ============================================================== */}
      {modalAuditoriaAbierto && ordenParaAuditar && (
        <div className="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-center justify-center p-4 print:hidden">
          <div className="bg-[#18181b] border border-red-900/60 w-full max-w-md rounded-2xl p-5 space-y-4">
            <div className="flex justify-between items-center pb-2 border-b border-[#27272a]">
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
                className="w-full bg-[#09090b] border border-[#27272a] rounded-xl p-2.5 text-xs text-white focus:border-red-500 outline-none"
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

      {/* MODAL DE MERMA */}
      {modalMermaAbierto && (
        <div className="fixed inset-0 z-50 bg-black/80 backdrop-blur-sm flex items-center justify-center p-4 print:hidden">
          <div className="bg-[#18181b] border border-[#27272a] w-full max-w-sm rounded-2xl p-5 space-y-4">
            <div className="flex justify-between items-center pb-2 border-b border-[#27272a]">
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
                  className="w-full bg-[#09090b] border border-[#27272a] rounded-xl p-2.5 text-white"
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
                  className="w-full bg-[#09090b] border border-[#27272a] rounded-xl p-2.5 text-white font-bold"
                />
              </div>

              <button
                onClick={() => {
                  const insumoId = Number((document.getElementById('select-merma-insumo') as HTMLSelectElement).value);
                  const cant = Number((document.getElementById('input-merma-cant') as HTMLInputElement).value) || 1;
                  const key = `${sedeActivaId}_${insumoId}`;
                  setInventarioSedes(prev => ({
                    ...prev,
                    [key]: Math.max(0, (prev[key] ?? 0) - cant)
                  }));
                  setModalMermaAbierto(false);
                  alert(`Merma registrada: -${cant} unidades descontadas del inventario.`);
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

    const root = ReactDOM.createRoot(document.getElementById('root'));
    root.render(<App />);
  </script>
</body>
</html>
"""

@app.route("/", methods=["GET"])
def index():
    return render_template_string("{% raw %}\n" + HTML_TEMPLATE + "\n{% endraw %}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    print(f">> Iniciando DoggiPOS Unificado en http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
