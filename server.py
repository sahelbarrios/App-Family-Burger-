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