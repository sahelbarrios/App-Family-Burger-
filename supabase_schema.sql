-- ========================================================================
-- ESQUEMA RELACIONAL COMPLETO - DOGGIPOS MULTISEDE & AUDITORÍA (PRODUCCIÓN)
-- Compatible con PostgreSQL / Supabase
-- ========================================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- 1. TABLA: configuracion
CREATE TABLE IF NOT EXISTS configuracion (
    id SERIAL PRIMARY KEY,
    tasa_bcv NUMERIC(12, 4) NOT NULL DEFAULT 45.5000,
    tasa_manual NUMERIC(12, 4) NOT NULL DEFAULT 46.0000,
    tasa_reposicion NUMERIC(12, 4) NOT NULL DEFAULT 47.5000,
    usar_tasa_manual BOOLEAN NOT NULL DEFAULT FALSE,
    pct_reserva_operativa NUMERIC(5, 2) NOT NULL DEFAULT 10.00,
    gastos_operativos_fijos NUMERIC(12, 2) NOT NULL DEFAULT 0.00,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- 2. TABLA: sedes
CREATE TABLE IF NOT EXISTS sedes (
    id SERIAL PRIMARY KEY,
    nombre VARCHAR(120) NOT NULL,
    es_almacen BOOLEAN NOT NULL DEFAULT FALSE,
    activa BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- 3. TABLA: usuarios
CREATE TABLE IF NOT EXISTS usuarios (
    id SERIAL PRIMARY KEY,
    nombre VARCHAR(120) NOT NULL,
    username VARCHAR(60) UNIQUE NOT NULL,
    pin_plain VARCHAR(20) NOT NULL DEFAULT '1234',
    pin_hash VARCHAR(255) NOT NULL,
    rol VARCHAR(20) NOT NULL CHECK (rol IN ('superusuario', 'cajero')),
    sede_id INT REFERENCES sedes(id) ON DELETE SET NULL,
    activo BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- 4. TABLA: insumos_maestro
CREATE TABLE IF NOT EXISTS insumos_maestro (
    id SERIAL PRIMARY KEY,
    nombre VARCHAR(120) NOT NULL,
    unidad VARCHAR(30) NOT NULL DEFAULT 'und',
    costo_paquete_usd NUMERIC(12, 4) NOT NULL,
    rendimiento_paquete NUMERIC(12, 4) NOT NULL CHECK (rendimiento_paquete > 0),
    costo_unitario_usd NUMERIC(12, 4) NOT NULL,
    alerta_stock_minimo NUMERIC(12, 2) NOT NULL DEFAULT 10,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- 5. TABLA: inventario_por_sede
CREATE TABLE IF NOT EXISTS inventario_por_sede (
    id SERIAL PRIMARY KEY,
    sede_id INT NOT NULL REFERENCES sedes(id) ON DELETE CASCADE,
    insumo_id INT NOT NULL REFERENCES insumos_maestro(id) ON DELETE CASCADE,
    stock_actual NUMERIC(12, 2) NOT NULL DEFAULT 0,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    CONSTRAINT uq_sede_insumo UNIQUE (sede_id, insumo_id)
);

-- 6. TABLA: movimientos_kardex
CREATE TABLE IF NOT EXISTS movimientos_kardex (
    id SERIAL PRIMARY KEY,
    sede_id INT NOT NULL REFERENCES sedes(id) ON DELETE CASCADE,
    insumo_id INT NOT NULL REFERENCES insumos_maestro(id) ON DELETE CASCADE,
    usuario_id INT REFERENCES usuarios(id) ON DELETE SET NULL,
    tipo VARCHAR(30) NOT NULL CHECK (tipo IN ('COMPRA', 'VENTA', 'TRANSFERENCIA', 'MERMA', 'CONSUMO', 'REVERSION_AUDITORIA')),
    cantidad NUMERIC(12, 2) NOT NULL,
    costo_unitario_momento NUMERIC(12, 4) NOT NULL,
    motivo TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- 7. TABLA: catalogo_productos
CREATE TABLE IF NOT EXISTS catalogo_productos (
    id SERIAL PRIMARY KEY,
    nombre VARCHAR(120) NOT NULL,
    tipo VARCHAR(30) NOT NULL CHECK (tipo IN ('INDIVIDUAL', 'COMBO', 'BEBIDA', 'EXTRA')),
    precio_venta_usd NUMERIC(12, 2) NOT NULL,
    activo BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- 8. TABLA: recetas_bom
CREATE TABLE IF NOT EXISTS recetas_bom (
    id SERIAL PRIMARY KEY,
    producto_id INT NOT NULL REFERENCES catalogo_productos(id) ON DELETE CASCADE,
    insumo_id INT NOT NULL REFERENCES insumos_maestro(id) ON DELETE RESTRICT,
    cantidad_consumida NUMERIC(12, 2) NOT NULL,
    CONSTRAINT uq_receta_item UNIQUE (producto_id, insumo_id)
);

-- 9. TABLA: metodos_pago_qr
CREATE TABLE IF NOT EXISTS metodos_pago_qr (
    id SERIAL PRIMARY KEY,
    sede_id INT REFERENCES sedes(id) ON DELETE CASCADE,
    titulo VARCHAR(120) NOT NULL,
    banco VARCHAR(100) NOT NULL,
    telefono VARCHAR(30) NOT NULL,
    rif_cedula VARCHAR(30) NOT NULL,
    qr_base64 TEXT,
    activo BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- 10. TABLA: turnos_caja
CREATE TABLE IF NOT EXISTS turnos_caja (
    id SERIAL PRIMARY KEY,
    sede_id INT NOT NULL REFERENCES sedes(id) ON DELETE RESTRICT,
    cajero_id INT NOT NULL REFERENCES usuarios(id) ON DELETE RESTRICT,
    apertura_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    cierre_at TIMESTAMP WITH TIME ZONE,
    fondo_inicial_usd NUMERIC(12, 2) NOT NULL DEFAULT 0,
    efectivo_declarado_cierre_usd NUMERIC(12, 2) DEFAULT NULL,
    diferencia_arqueo_usd NUMERIC(12, 2) DEFAULT NULL,
    estado VARCHAR(20) NOT NULL DEFAULT 'ABIERTO' CHECK (estado IN ('ABIERTO', 'CERRADO'))
);

-- 11. TABLA: ordenes y orden_items
CREATE TABLE IF NOT EXISTS ordenes (
    id SERIAL PRIMARY KEY,
    turno_id INT REFERENCES turnos_caja(id) ON DELETE SET NULL,
    sede_id INT NOT NULL REFERENCES sedes(id) ON DELETE RESTRICT,
    cajero_id INT NOT NULL REFERENCES usuarios(id) ON DELETE RESTRICT,
    cliente_nombre VARCHAR(120),
    cliente_telefono VARCHAR(40),
    total_venta_usd NUMERIC(12, 2) NOT NULL,
    costo_insumos_usd NUMERIC(12, 4) NOT NULL DEFAULT 0,
    tasa_aplicada NUMERIC(12, 4) NOT NULL,
    tasa_reposicion_momento NUMERIC(12, 4) NOT NULL,
    recibido_usd NUMERIC(12, 2) NOT NULL DEFAULT 0,
    recibido_bs NUMERIC(14, 2) NOT NULL DEFAULT 0,
    vuelto_usd NUMERIC(12, 2) NOT NULL DEFAULT 0,
    vuelto_bs NUMERIC(14, 2) NOT NULL DEFAULT 0,
    metodo_qr_id INT REFERENCES metodos_pago_qr(id) ON DELETE SET NULL,
    referencia_pago VARCHAR(80),
    estado_pago VARCHAR(20) NOT NULL DEFAULT 'PAGADA' CHECK (estado_pago IN ('PAGADA', 'FIADO', 'ANULADA')),
    estado_cocina VARCHAR(20) NOT NULL DEFAULT 'PREPARANDO' CHECK (estado_cocina IN ('PREPARANDO', 'ENTREGADO')),
    notas_cocina TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS orden_items (
    id SERIAL PRIMARY KEY,
    orden_id INT NOT NULL REFERENCES ordenes(id) ON DELETE CASCADE,
    producto_id INT NOT NULL REFERENCES catalogo_productos(id) ON DELETE RESTRICT,
    cantidad INT NOT NULL CHECK (cantidad > 0),
    precio_unitario_usd NUMERIC(12, 2) NOT NULL,
    costo_unitario_usd NUMERIC(12, 4) NOT NULL,
    modificadores_json TEXT DEFAULT '[]'
);

-- 12. TABLA: auditoria_logs
CREATE TABLE IF NOT EXISTS auditoria_logs (
    id SERIAL PRIMARY KEY,
    superusuario_id INT REFERENCES usuarios(id) ON DELETE SET NULL,
    sede_id INT REFERENCES sedes(id) ON DELETE SET NULL,
    accion VARCHAR(40) NOT NULL CHECK (accion IN ('ANULAR_ORDEN', 'EDITAR_ORDEN', 'AJUSTE_STOCK', 'CAMBIO_PRECIO', 'MERMA_DECLARADA', 'TRANSFERENCIA')),
    entidad_afectada VARCHAR(60) NOT NULL,
    registro_id INT,
    snapshot_anterior_json TEXT,
    snapshot_nuevo_json TEXT,
    justificacion TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- ========================================================================
-- DATOS SEMILLA EXACTOS Y PINs REALES VALIDADOS
-- ========================================================================

INSERT INTO configuracion (id, tasa_bcv, tasa_manual, tasa_reposicion, usar_tasa_manual, pct_reserva_operativa, gastos_operativos_fijos)
VALUES (1, 45.5000, 46.0000, 47.5000, FALSE, 10.00, 0.00)
ON CONFLICT (id) DO NOTHING;

INSERT INTO sedes (id, nombre, es_almacen, activa) VALUES
(1, 'Almacén Central', TRUE, TRUE),
(2, 'Sede 1 (Puesto Principal)', FALSE, TRUE),
(3, 'Sede 2 (Puesto Norte)', FALSE, TRUE)
ON CONFLICT (id) DO NOTHING;

-- PINs Reales: Superadmin = 1234 | Cajero 1 = 0001 | Cajero 2 = 0002
INSERT INTO usuarios (id, nombre, username, pin_plain, pin_hash, rol, sede_id, activo) VALUES
(1, 'Sahel Barrios (Superusuario)', 'superadmin', '1234', 'scrypt:32768:8:1$rKu1DqRBVBSxxOBh$99fc561956e43af765f2154adabd6bb5a164ed8a7dca7dbdda55ded7ac9f89a1754b3d17a6af576ff779eb6632f404aba6019ba0fd9988259289e38ab2d11d7f', 'superusuario', NULL, TRUE),
(2, 'Carlos Cajero', 'cajero1', '0001', 'scrypt:32768:8:1$dy3iAjBUBkDdDkKl$b71c262e0edcf5cf02bfca6b021df74ecf20a36626ae63df45945d54b3628f20514183399f27bc60b255e0ac3b68dc477e4519efed3ba9f5dc148390241b8576', 'cajero', 2, TRUE),
(3, 'Ana Cajera', 'cajero2', '0002', 'scrypt:32768:8:1$CbjPI2mXP63YW1Ko$60f3fc8c722693f39c82234a9f8995712edc1319c02756a3e13832432f2b8f2853f73e6f3b12d746dbf862be1cb6503fc74790c7eb2bcaa38005b79e0136f0c3', 'cajero', 3, TRUE)
ON CONFLICT (id) DO NOTHING;

INSERT INTO insumos_maestro (id, nombre, unidad, costo_paquete_usd, rendimiento_paquete, costo_unitario_usd, alerta_stock_minimo) VALUES
(1, 'Pan Pequeño Estándar', 'und', 1.98, 10, 0.1980, 10),
(2, 'Pan Yumpi Grande', 'und', 2.30, 10, 0.2300, 5),
(3, 'Salchicha Recado', 'und', 3.00, 10, 0.3000, 15),
(4, 'Salsa de Tomate', 'porcion', 5.00, 250, 0.0200, 30),
(5, 'Mayonesa', 'porcion', 5.00, 100, 0.0500, 25),
(6, 'Mostaza', 'porcion', 3.00, 300, 0.0100, 30),
(7, 'Base para Salsa', 'porcion', 6.00, 150, 0.0400, 20),
(8, 'Verduras (Cebolla, Repollo, Cilantro)', 'porcion', 4.00, 60, 0.0667, 15),
(9, 'Papitas Ralladas', 'porcion', 3.76, 100, 0.0376, 25),
(10, 'Papel Envoltorio / Servilleta', 'und', 2.00, 100, 0.0200, 25),
(11, 'Refresco Glu 400ml', 'und', 5.90, 15, 0.3933, 5),
(12, 'Refresco 1 Litro', 'und', 5.00, 6, 0.8333, 3)
ON CONFLICT (id) DO NOTHING;

INSERT INTO catalogo_productos (id, nombre, tipo, precio_venta_usd, activo) VALUES
(1, 'Perro Pequeño Clásico', 'INDIVIDUAL', 1.50, TRUE),
(2, 'Perro Grande Yumpi Especial', 'INDIVIDUAL', 2.00, TRUE),
(3, 'Combo Dúo Clásico (2 Peq + 1 Glu)', 'COMBO', 3.00, TRUE),
(4, 'Combo Yumpi Individual (1 Yumpi + 1 Glu)', 'COMBO', 2.50, TRUE),
(5, 'Combo Yumpi Doble (2 Yumpi + 1 Glu)', 'COMBO', 4.00, TRUE),
(6, 'Combo Familiar (4 Peq + Refresco 1L)', 'COMBO', 6.00, TRUE),
(7, 'Refresco Glu 400ml', 'BEBIDA', 1.00, TRUE),
(8, 'Refresco 1 Litro', 'BEBIDA', 1.75, TRUE),
(9, 'Extra Salchicha', 'EXTRA', 0.60, TRUE),
(10, 'Extra Papitas / Queso', 'EXTRA', 0.50, TRUE)
ON CONFLICT (id) DO NOTHING;

INSERT INTO recetas_bom (producto_id, insumo_id, cantidad_consumida) VALUES
-- 1. Perro Pequeño Clásico ($0.74 costo)
(1, 1, 1), (1, 3, 1), (1, 4, 1), (1, 5, 1), (1, 6, 1), (1, 7, 1), (1, 8, 1), (1, 9, 1), (1, 10, 1),
-- 2. Perro Grande Yumpi ($0.77 costo)
(2, 2, 1), (2, 3, 1), (2, 4, 1), (2, 5, 1), (2, 6, 1), (2, 7, 1), (2, 8, 1), (2, 9, 1), (2, 10, 1),
-- 3. Combo Dúo Clásico (2 Peq + 1 Glu = $1.88 costo)
(3, 1, 2), (3, 3, 2), (3, 4, 2), (3, 5, 2), (3, 6, 2), (3, 7, 2), (3, 8, 2), (3, 9, 2), (3, 10, 2), (3, 11, 1),
-- 4. Combo Yumpi Individual (1 Yumpi + 1 Glu = $1.16 costo)
(4, 2, 1), (4, 3, 1), (4, 4, 1), (4, 5, 1), (4, 6, 1), (4, 7, 1), (4, 8, 1), (4, 9, 1), (4, 10, 1), (4, 11, 1),
-- 5. Combo Yumpi Doble (2 Yumpi + 1 Glu = $1.94 costo)
(5, 2, 2), (5, 3, 2), (5, 4, 2), (5, 5, 2), (5, 6, 2), (5, 7, 2), (5, 8, 2), (5, 9, 2), (5, 10, 2), (5, 11, 1),
-- 6. Combo Familiar (4 Peq + 1L = $3.80 costo)
(6, 1, 4), (6, 3, 4), (6, 4, 4), (6, 5, 4), (6, 6, 4), (6, 7, 4), (6, 8, 4), (6, 9, 4), (6, 10, 4), (6, 12, 1),
-- Bebidas y Extras
(7, 11, 1),
(8, 12, 1),
(9, 3, 1),
(10, 9, 1)
ON CONFLICT (producto_id, insumo_id) DO NOTHING;

-- Stock inicial de tu primera tanda de 30 perros en Sede 1 (20 pan peq, 10 Yumpi, 30 salchichas, 15 Glu)
INSERT INTO inventario_por_sede (sede_id, insumo_id, stock_actual) VALUES
(2, 1, 20), (2, 2, 10), (2, 3, 30), (2, 4, 250), (2, 5, 100), (2, 6, 300), (2, 7, 150), (2, 8, 60), (2, 9, 100), (2, 10, 100), (2, 11, 15), (2, 12, 6),
(3, 1, 20), (3, 2, 10), (3, 3, 30), (3, 4, 250), (3, 5, 100), (3, 6, 300), (3, 7, 150), (3, 8, 60), (3, 9, 100), (3, 10, 100), (3, 11, 15), (3, 12, 6),
(1, 1, 100), (1, 2, 50), (1, 3, 150), (1, 4, 500), (1, 5, 300), (1, 6, 600), (1, 7, 300), (1, 8, 180), (1, 9, 300), (1, 10, 300), (1, 11, 60), (1, 12, 24)
ON CONFLICT (sede_id, insumo_id) DO NOTHING;

INSERT INTO metodos_pago_qr (id, sede_id, titulo, banco, telefono, rif_cedula, qr_base64, activo)
VALUES (1, NULL, 'Pago Móvil Principal', 'Banesco (0134)', '0424-5371079', 'V-19000000', '', TRUE)
ON CONFLICT (id) DO NOTHING;
