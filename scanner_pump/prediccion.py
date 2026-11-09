"""Predicción: monedas del escaneo cuyos movimientos antes del escaneo se parecen a los de las que después subieron.

**La huella.** Para cada moneda escaneada (vela diaria con +10 % o más) se toman las velas de 2H, 4H, 8H y 12H
hasta el momento del escaneo (el cierre de la vela de la explosión, 00:00 UTC = 19:00 hora Lima), 84 velas de
cada una (7, 14, 28 y 42 días). Las de 4H, 8H y 12H se construyen con las de 2H, que empiezan a las 00:00 UTC
igual que las de Binance. De cada temporalidad salen:

- la forma del precio (el cierre de cada tramo frente al último cierre, en 12 puntos);
- el volumen (cómo se reparte a lo largo del tramo, en 6 puntos);
- la volatilidad (rango medio de las velas) y la tendencia (variación del tramo);
- los **fractales** de 5 velas (un techo es una vela con el máximo más alto que las dos anteriores y las dos
  siguientes; un suelo, con el mínimo más bajo): cuántos hay, si los suelos y los techos van subiendo, la
  distancia del precio al último techo y al último suelo, y si ya rompió el último techo. Solo cuentan los
  fractales confirmados antes del escaneo (los de las dos últimas velas aún no lo están).

**El resultado.** Llegó si el máximo de los 10 días siguientes al escaneo sube +50 % sobre el cierre de la vela de
la explosión (lo que en el seguimiento es «Máx después», con un plazo de 10 días).

**La puntuación.** De las ``VECINAS`` explosiones pasadas (ya con resultado) cuya huella más se parece, qué parte
llegó a +50 %. Se compara con todas, también con las que no subieron: una huella que tienen muchas ganadoras pero
también muchas que no subieron no puntúa alto.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Sequence

import numpy as np

PASO_2H = 2 * 3600 * 1000
DIA_MS = 86_400_000
TEMPORALIDADES = {"2h": 1, "4h": 2, "8h": 4, "12h": 6}  # cuántas velas de 2H forman una
VELAS = 84
MIN_VELAS = 30  # con menos velas en alguna temporalidad (moneda muy nueva) no hay huella
PUNTOS_FORMA = 12
PUNTOS_VOLUMEN = 6
VENTANA_DIAS = 10
OBJETIVO = 50.0  # %
VECINAS = 50
ESCALARES = ["volatilidad", "tendencia", "techos", "suelos", "suelos_suben", "techos_suben", "dist_techo",
             "dist_suelo", "rompe_techo"]


def nombres() -> list[str]:
    """Nombre de cada valor de la huella, en orden."""
    out = []
    for tf in TEMPORALIDADES:
        out += [f"{tf}_forma_{i}" for i in range(PUNTOS_FORMA)]
        out += [f"{tf}_volumen_{i}" for i in range(PUNTOS_VOLUMEN)]
        out += [f"{tf}_{k}" for k in ESCALARES]
    return out


def agrupar(t: np.ndarray, ohlcv: np.ndarray, factor: int) -> tuple[np.ndarray, np.ndarray]:
    """Velas de 2H (apertura ms, [o, h, l, c, v]) → velas de ``factor`` × 2H alineadas con las 00:00 UTC."""
    if factor == 1 or not len(t):
        return t, ohlcv
    tamano = factor * PASO_2H
    grupo = t // tamano
    inicio = np.flatnonzero(np.r_[True, grupo[1:] != grupo[:-1]])
    fin = np.r_[inicio[1:], len(t)] - 1
    o, h, l, c, v = ohlcv.T
    out = np.column_stack([o[inicio], np.maximum.reduceat(h, inicio), np.minimum.reduceat(l, inicio), c[fin],
                           np.add.reduceat(v, inicio)])
    return grupo[inicio] * tamano, out


def _tramos(valores: np.ndarray, n: int) -> np.ndarray:
    return np.array([p.mean() for p in np.array_split(valores, n)])


def _fractales(h: np.ndarray, l: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Índices de los techos y suelos de 5 velas confirmados (con dos velas después)."""
    if len(h) < 5:
        return np.array([], int), np.array([], int)
    centro = np.arange(2, len(h) - 2)
    techo = np.ones(len(centro), bool)
    suelo = np.ones(len(centro), bool)
    for d in (-2, -1, 1, 2):
        techo &= h[centro] > h[centro + d]
        suelo &= l[centro] < l[centro + d]
    return centro[techo], centro[suelo]


def _de_temporalidad(velas: np.ndarray) -> list[float]:
    o, h, l, c, v = velas.T
    n = len(c)
    forma = _tramos(np.log(c / c[-1]), PUNTOS_FORMA)
    volumen = np.log(_tramos(v, PUNTOS_VOLUMEN) / max(v.mean(), 1e-12) + 1e-6)
    techos, suelos = _fractales(h, l)
    sube = lambda xs, serie: (1.0 if serie[xs[-1]] > serie[xs[-2]] else 0.0) if len(xs) >= 2 else 0.5
    ultimo_techo = h[techos[-1]] if len(techos) else h.max()
    ultimo_suelo = l[suelos[-1]] if len(suelos) else l.min()
    return list(forma) + list(volumen) + [
        float(np.mean(np.log(h / l))), float(np.log(c[-1] / c[0])), len(techos) / n, len(suelos) / n,
        sube(suelos, l), sube(techos, h), float(np.log(c[-1] / ultimo_techo)), float(np.log(c[-1] / ultimo_suelo)),
        1.0 if c[-1] > ultimo_techo else 0.0]


def agrupadas(t: np.ndarray, ohlcv: np.ndarray) -> dict[int, tuple[np.ndarray, np.ndarray]]:
    """Las velas de 2H agrupadas en cada temporalidad (para calcular muchas huellas de la misma moneda)."""
    return {factor: agrupar(t, ohlcv, factor) for factor in TEMPORALIDADES.values()}


def huella(t: np.ndarray, ohlcv: np.ndarray, escaneo: int,
           grupos: Optional[dict[int, tuple[np.ndarray, np.ndarray]]] = None) -> Optional[np.ndarray]:
    """La huella de una moneda en el momento ``escaneo`` (ms) con sus velas de 2H; ``None`` si no hay bastantes."""
    grupos = grupos or agrupadas(t, ohlcv)
    valores = []
    for factor in TEMPORALIDADES.values():
        ta, va = grupos[factor]
        hasta = np.searchsorted(ta, escaneo - factor * PASO_2H, side="right")  # solo velas cerradas antes
        va = va[max(0, hasta - VELAS):hasta]
        if len(va) < MIN_VELAS or (va[:, :4] <= 0).any():
            return None
        valores += _de_temporalidad(va)
    out = np.array(valores, dtype=np.float32)
    return out if np.isfinite(out).all() else None


def explosiones(t: np.ndarray, ohlcv: np.ndarray, min_pct: float = 10.0) -> list[dict]:
    """Velas diarias (de las de 2H) con +``min_pct`` % de mínimo a máximo o de apertura a cierre, y el máximo de
    los 10 días siguientes al escaneo frente al cierre (``None`` si aún no pasaron)."""
    td, vd = agrupar(t, ohlcv, 12)
    out = []
    for i, (abre, (o, h, l, c, v)) in enumerate(zip(td, vd)):
        rango, cuerpo = (h / l - 1) * 100 if l > 0 else 0.0, (c / o - 1) * 100 if o > 0 else 0.0
        if rango < min_pct and cuerpo < min_pct:
            continue
        escaneo = int(abre) + DIA_MS
        despues = vd[i + 1:i + 1 + VENTANA_DIAS]
        # Los 10 días seguidos y ya cerrados (la última vela de 2H termina después del décimo día).
        completo = (len(despues) == VENTANA_DIAS and td[i + VENTANA_DIAS] == abre + VENTANA_DIAS * DIA_MS
                    and t[-1] + PASO_2H >= escaneo + VENTANA_DIAS * DIA_MS)
        maximo = (despues[:, 1].max() / c - 1) * 100 if len(despues) else None
        out.append({"apertura": int(abre), "escaneo": escaneo, "pct_rango": float(rango), "pct_cuerpo": float(cuerpo),
                    "cierre": float(c), "max_despues": float(maximo) if completo else None})
    return out


class Biblioteca:
    """Huellas pasadas con resultado; puntúa las nuevas por sus vecinas más parecidas."""

    def __init__(self, x: np.ndarray, llego: np.ndarray, info: Optional[list] = None) -> None:
        self.media = x.mean(axis=0)
        self.escala = x.std(axis=0) + 1e-6
        self.x = ((x - self.media) / self.escala).astype(np.float32)
        self.llego = llego.astype(bool)
        self.info = info
        self._normas = (self.x ** 2).sum(axis=1)

    def vecinas(self, consultas: np.ndarray, k: Optional[int] = None) -> tuple[np.ndarray, np.ndarray]:
        """Índices y distancias de las ``k`` huellas más parecidas a cada consulta."""
        k = min(k or VECINAS, len(self.x))
        q = ((consultas - self.media) / self.escala).astype(np.float32)
        indices, distancias = [], []
        for desde in range(0, len(q), 512):
            bloque = q[desde:desde + 512]
            d2 = (bloque ** 2).sum(axis=1)[:, None] + self._normas[None, :] - 2 * bloque @ self.x.T
            idx = np.argpartition(d2, k - 1, axis=1)[:, :k]
            orden = np.take_along_axis(d2, idx, axis=1).argsort(axis=1)
            idx = np.take_along_axis(idx, orden, axis=1)
            indices.append(idx)
            distancias.append(np.sqrt(np.maximum(np.take_along_axis(d2, idx, axis=1), 0)))
        return np.vstack(indices), np.vstack(distancias)

    def puntuar(self, consultas: np.ndarray, k: Optional[int] = None) -> tuple[np.ndarray, np.ndarray]:
        """Parte de las ``k`` vecinas que llegaron a +50 % (y los índices de las vecinas)."""
        idx, _ = self.vecinas(consultas, k)
        return self.llego[idx].mean(axis=1), idx


# --- Velas de 2H en caché ------------------------------------------------------------------------------

DESDE_CACHE = 1668470400000  # 15-11-2022: 42 días antes de 2023, para la huella de 12H


def leer_cache(ruta: Path) -> tuple[np.ndarray, np.ndarray]:
    if not ruta.exists():
        return np.zeros(0, np.int64), np.zeros((0, 5), np.float32)
    d = np.load(ruta)
    return d["t"], d["ohlcv"]


def actualizar_par(cliente, simbolo: str, carpeta: Path, ahora: int) -> int:
    """Pide a Binance las velas de 2H que faltan en la caché del par (todas desde ``DESDE_CACHE`` la primera vez)."""
    ruta = carpeta / cliente.market.key / f"{simbolo}.npz"
    t, v = leer_cache(ruta)
    desde = int(t[-1]) + PASO_2H if len(t) else DESDE_CACHE
    nuevas = []
    while desde + PASO_2H <= ahora:
        falta = (ahora - desde) // PASO_2H + 1
        k = cliente.klines(simbolo, "2h", int(min(cliente.market.max_klines, falta)), start_time=desde)
        nuevas += [f for f in k if int(f[0]) + PASO_2H <= ahora]  # solo velas cerradas
        if len(k) < min(cliente.market.max_klines, falta):
            break
        desde = int(k[-1][0]) + PASO_2H
    if nuevas:
        a = np.array([[float(x) for x in f[:6]] for f in nuevas])
        t = np.concatenate([t, a[:, 0].astype(np.int64)])
        v = np.concatenate([v, a[:, 1:6].astype(np.float32)])
        ruta.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(ruta, t=t, ohlcv=v)
    return len(nuevas)


def actualizar_cache(carpeta: Path, ahora: Optional[int] = None, hilos: int = 8) -> list[str]:
    """Pone al día las velas de 2H de todos los pares que escanea el escáner. Devuelve los avisos."""
    from .binance import MARKETS, BinanceClient, BinanceError
    from .scanner import is_tokenized_stock, select_symbols

    ahora = ahora or int(datetime.now(timezone.utc).timestamp() * 1000)
    avisos = []
    for key in ("spot", "futures"):
        cliente = BinanceClient(MARKETS[key])
        try:
            pares = select_symbols(key, cliente.exchange_info())
        except BinanceError as exc:
            avisos.append(f"{cliente.market.label}: {exc}")
            continue
        if key == "spot":
            tags = cliente.asset_tags() or {}
            pares = [p for p in pares if not is_tokenized_stock(tags.get(p.symbol, ()))]

        def uno(par):
            try:
                return actualizar_par(cliente, par.symbol, carpeta, ahora)
            except BinanceError as exc:
                return exc

        with ThreadPoolExecutor(max_workers=hilos) as pool:
            fallos = sum(isinstance(r, Exception) for r in pool.map(uno, pares))
        if fallos:
            avisos.append(f"{cliente.market.label}: {fallos} pares sin actualizar")
        print(f"{cliente.market.label}: velas de 2H al día de {len(pares) - fallos} pares", file=sys.stderr)
    return avisos


# --- Biblioteca de explosiones pasadas -----------------------------------------------------------------

DESDE_BIBLIOTECA = 1672531200000  # 2023-01-01


def _explosiones_de(ruta: Path) -> tuple[list[dict], list[np.ndarray]]:
    t, v = leer_cache(ruta)
    v = v.astype(np.float64)
    if len(t) < 600:
        return [], []
    grupos = agrupadas(t, v)
    filas, huellas = [], []
    for e in explosiones(t, v):
        if e["escaneo"] < DESDE_BIBLIOTECA or e["max_despues"] is None:
            continue
        h = huella(t, v, e["escaneo"], grupos)
        if h is not None:
            filas.append({"mercado": ruta.parent.name, "simbolo": ruta.stem, **e})
            huellas.append(h)
    return filas, huellas


def construir_biblioteca(carpeta: Path) -> tuple[list[dict], np.ndarray]:
    """Todas las explosiones desde 2023 de la caché que ya tienen resultado (10 días), con su huella."""
    filas, huellas = [], []
    rutas = sorted(carpeta.glob("*/*.npz"))
    with ProcessPoolExecutor() as pool:
        for f, h in pool.map(_explosiones_de, rutas, chunksize=8):
            filas += f
            huellas += h
    return filas, (np.vstack(huellas) if huellas else np.zeros((0, len(nombres())), np.float32))


def _rango_del_dia(valores: np.ndarray) -> np.ndarray:
    """Posición de cada valor entre los del día (0 a 1)."""
    if len(valores) <= 1:
        return np.ones(len(valores))
    orden = valores.argsort().argsort()
    return orden / (len(valores) - 1)


def combinar(parecido: np.ndarray, tamano: np.ndarray) -> np.ndarray:
    """Puntuación del día: la mitad por la huella (cuántas parecidas subieron) y la mitad por el tamaño de la vela.
    En la prueba desde 2024, juntas acertaron más que cada una por separado."""
    return 0.5 * _rango_del_dia(parecido) + 0.5 * _rango_del_dia(tamano)


GRUPOS = {"rango": ("pct_rango", "Mín→Máx"), "cuerpo": ("pct_cuerpo", "Apertura→Cierre")}


def predecir_dia(dia: dict, carpeta: Path, filas: list[dict], x: np.ndarray) -> dict:
    """Puntúa las monedas de un día del seguimiento con las explosiones que ya tenían resultado ese día."""
    monedas = [c for c in dia.get("monedas", []) if c.get("apertura") and c.get("simbolo")]
    escaneo = min((int(c["apertura"]) + DIA_MS for c in monedas), default=0)
    conocido = np.array([f["escaneo"] + VENTANA_DIAS * DIA_MS <= escaneo for f in filas], dtype=bool)
    out = {"fecha": dia["fecha"], "escaneo_utc": datetime.fromtimestamp(escaneo / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if escaneo else None,
           "calculada": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "biblioteca": {"explosiones": int(conocido.sum()),
                          "llegaron": int(sum(f["max_despues"] >= OBJETIVO for f, k in zip(filas, conocido) if k))},
           "monedas": []}
    con_huella, huellas = [], []
    for c in monedas:
        t, v = leer_cache(carpeta / c["mercado"] / f"{c['simbolo']}.npz")
        h = huella(t, v.astype(np.float64), int(c["apertura"]) + DIA_MS) if len(t) else None
        fila = {k: c.get(k) for k in ("mercado", "simbolo", "base", "quote", "fecha", "fecha_analisis", "apertura",
                                      "open", "high", "low", "close", "volumen", "pct_rango", "pct_cuerpo", "analisis")}
        fila.update({"parecido": None, "vecinas": [], "puntuacion": {}})
        out["monedas"].append(fila)
        if h is not None:
            con_huella.append(fila)
            huellas.append(h)
    if con_huella and conocido.sum() > VECINAS:
        indices = np.flatnonzero(conocido)
        biblio = Biblioteca(x[indices], np.array([filas[i]["max_despues"] >= OBJETIVO for i in indices]))
        p, vecinas = biblio.puntuar(np.vstack(huellas))
        for fila, pi, vi in zip(con_huella, p, vecinas):
            fila["parecido"] = round(float(pi), 4)
            fila["vecinas"] = [[filas[indices[j]]["simbolo"], filas[indices[j]]["mercado"],
                                filas[indices[j]]["escaneo"], round(filas[indices[j]]["max_despues"], 1)] for j in vi[:3]]
        for grupo, (campo, _) in GRUPOS.items():
            del_grupo = [f for f in con_huella if (f.get(campo) or 0) >= 10]
            if del_grupo:
                puntos = combinar(np.array([f["parecido"] for f in del_grupo]), np.array([f[campo] for f in del_grupo]))
                for f, s in zip(del_grupo, puntos):
                    f["puntuacion"][grupo] = round(float(s), 4)
    return out


PRIMERA_DEL_DIA = "2026-11-08T00:00:00Z"  # escaneo de la primera predicción calculada el mismo día


def reconstruida(prediccion: dict) -> bool:
    """¿Se calculó después (más de un día tras el escaneo, con los datos que había ese día) y no el mismo día?"""
    calculada, escaneo = prediccion.get("calculada"), prediccion.get("escaneo_utc")
    if not escaneo:
        return True
    if not calculada:  # las primeras del workflow diario no guardaban la hora; las reconstruidas acaban antes
        return escaneo < PRIMERA_DEL_DIA
    a = datetime.fromisoformat(calculada.replace("Z", "+00:00"))
    return a - datetime.fromisoformat(escaneo.replace("Z", "+00:00")) > timedelta(days=1)


def resumen_dia(prediccion: dict) -> dict:
    """Por medida: cuántas monedas, cuántas de las 5 mejor puntuadas llegaron a +50 % y la media del día
    (solo con las que ya tienen resultado a 10 días)."""
    out = {}
    for grupo, (campo, _) in GRUPOS.items():
        cs = [c for c in prediccion.get("monedas", []) if grupo in (c.get("puntuacion") or {})]
        if not cs:
            continue
        cs.sort(key=lambda c: -c["puntuacion"][grupo])
        con = [c for c in cs if c.get("max_despues") is not None]
        top = [c for c in cs[:5] if c.get("max_despues") is not None]
        out[grupo] = {"monedas": len(cs), "con_resultado": len(con),
                      "top5": {"llegaron": sum(c["max_despues"] >= OBJETIVO for c in top), "de": len(top)},
                      "base": round(sum(c["max_despues"] >= OBJETIVO for c in con) / len(con), 4) if con else None}
    return out


def en_vivo(predicciones: list[dict]) -> dict:
    """Cómo acertaron las predicciones de los días que ya tienen resultado (10 días después)."""
    res = {}
    for grupo, (campo, nombre) in GRUPOS.items():
        top, grandes, mejor, base, monedas = [], [], [], [], 0
        for pred in predicciones:
            cs = [c for c in pred.get("monedas", []) if grupo in (c.get("puntuacion") or {}) and c.get("max_despues") is not None]
            if len(cs) < 5:
                continue
            y = np.array([c["max_despues"] >= OBJETIVO for c in cs])
            orden = np.argsort([-c["puntuacion"][grupo] for c in cs], kind="stable")
            top.append(y[orden[:5]].mean())
            mejor.append(float(y[orden[0]]))
            grandes.append(y[np.argsort([-c[campo] for c in cs], kind="stable")[:5]].mean())
            base.append(y.mean())
            monedas += len(cs)
        if top:
            res[grupo] = {"nombre": nombre, "dias": len(top), "monedas": monedas, "base": round(float(np.mean(base)), 4),
                          "top5": round(float(np.mean(top)), 4), "top5_velas_grandes": round(float(np.mean(grandes)), 4),
                          "mejor_del_dia": round(float(np.mean(mejor)), 4)}
    return res


def completar_resultados(prediccion: dict, filas_por_clave: dict) -> bool:
    """Añade el máximo de los 10 días siguientes a las monedas que ya lo tienen. Devuelve si cambió algo."""
    cambio = False
    for c in prediccion.get("monedas", []):
        if c.get("max_despues") is not None or not c.get("apertura"):
            continue
        e = filas_por_clave.get((c["mercado"], c["simbolo"], int(c["apertura"]) + DIA_MS))
        if e is not None:
            c["max_despues"] = round(e["max_despues"], 2)
            cambio = True
    return cambio


# --- Prueba hacia atrás ----------------------------------------------------------------------------------

def probar(filas: list[dict], x: np.ndarray, desde: str = "2024-01-01") -> dict:
    """Mes a mes, sin mirar el futuro: la biblioteca solo tiene las explosiones cuyo resultado se conocía 10 días
    antes de empezar el mes. Compara la puntuación con elegir al azar y con elegir las velas más grandes."""
    escaneo = np.array([f["escaneo"] for f in filas], dtype=np.int64)
    llego = np.array([f["max_despues"] >= OBJETIVO for f in filas])
    dia = escaneo // DIA_MS
    p = np.full(len(filas), np.nan)
    inicio = datetime.fromisoformat(desde).replace(tzinfo=timezone.utc)
    mes = inicio
    fin = escaneo.max() if len(escaneo) else 0
    while int(mes.timestamp() * 1000) <= fin:
        siguiente = (mes.replace(day=28) + timedelta(days=4)).replace(day=1)
        a, b = int(mes.timestamp() * 1000), int(siguiente.timestamp() * 1000)
        prueba = (escaneo >= a) & (escaneo < b)
        conocido = escaneo + VENTANA_DIAS * DIA_MS <= a
        if prueba.any() and conocido.sum() > VECINAS:
            p[prueba], _ = Biblioteca(x[conocido], llego[conocido]).puntuar(x[prueba])
        mes = siguiente
    res = {"desde": desde, "hasta": datetime.fromtimestamp(fin / 1000, timezone.utc).date().isoformat() if fin else None,
           "objetivo": OBJETIVO, "ventana_dias": VENTANA_DIAS, "grupos": {}}
    for grupo, (campo, nombre) in GRUPOS.items():
        m = ~np.isnan(p) & (np.array([f[campo] for f in filas]) >= 10)
        if not m.any():
            continue
        g_dia, g_p, g_y = dia[m], p[m], llego[m]
        g_t = np.array([f[campo] for f in filas])[m]
        top = {"combinada": [], "huella": [], "velas_grandes": []}
        mejor = {"combinada": [], "velas_grandes": []}
        por_anio: dict[str, list] = {}
        for d in np.unique(g_dia):
            s = g_dia == d
            puntos = combinar(g_p[s], g_t[s])
            for nombre_, valores in (("combinada", puntos), ("huella", g_p[s]), ("velas_grandes", g_t[s])):
                elegidas = np.argsort(-valores, kind="stable")[:5]
                top[nombre_].append(g_y[s][elegidas].mean())
                if nombre_ in mejor:
                    mejor[nombre_].append(float(g_y[s][elegidas[0]]))
            anio = datetime.fromtimestamp(d * 86400, timezone.utc).year
            por_anio.setdefault(str(anio), []).append(g_y[s][np.argsort(-puntos, kind="stable")[:5]].mean())
        tramos = [0, 0.02, 0.05, 0.1, 0.2, 0.3, 1.01]
        calibracion = []
        for lo, hi in zip(tramos[:-1], tramos[1:]):
            s = (g_p >= lo) & (g_p < hi)
            if s.any():
                calibracion.append({"desde": lo, "hasta": min(hi, 1.0), "explosiones": int(s.sum()), "llegaron": round(float(g_y[s].mean()), 4)})
        res["grupos"][grupo] = {
            "nombre": nombre, "explosiones": int(m.sum()), "dias": int(len(np.unique(g_dia))), "base": round(float(g_y.mean()), 4),
            "top5": {k: round(float(np.mean(v)), 4) for k, v in top.items()},
            "mejor_del_dia": {k: round(float(np.mean(v)), 4) for k, v in mejor.items()},
            "top5_por_anio": {k: round(float(np.mean(v)), 4) for k, v in por_anio.items()},
            "calibracion": calibracion}
    return res


# --- Línea de comandos -----------------------------------------------------------------------------------

def _leer_json(ruta: Path) -> Optional[dict]:
    try:
        return json.loads(ruta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _escribir_json(ruta: Path, datos: dict) -> None:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(json.dumps(datos, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scanner_pump.prediccion", description=__doc__.splitlines()[0])
    parser.add_argument("--seguimiento", type=Path, default=Path("datos/seguimiento"))
    parser.add_argument("--salida", type=Path, default=Path("datos/prediccion"))
    parser.add_argument("--cache", type=Path, default=Path("cache/velas-2h"), help="velas de 2H (no van al repositorio)")
    parser.add_argument("--fecha", nargs="*", default=[], help="días del seguimiento a predecir (por defecto, el último)")
    parser.add_argument("--probar", action="store_true", help="repetir la prueba hacia atrás (prueba.json)")
    parser.add_argument("--sin-actualizar", action="store_true", help="no pedir velas nuevas a Binance")
    args = parser.parse_args(argv)

    avisos = [] if args.sin_actualizar else actualizar_cache(args.cache)
    filas, x = construir_biblioteca(args.cache)
    print(f"Biblioteca: {len(filas)} explosiones con resultado, "
          f"{sum(f['max_despues'] >= OBJETIVO for f in filas)} llegaron a +{OBJETIVO:.0f} %", file=sys.stderr)
    dias = sorted(p.stem for p in args.seguimiento.glob("*.json") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.stem))
    pedidas = args.fecha or dias[-1:]
    for fecha in pedidas:
        dia = _leer_json(args.seguimiento / f"{fecha}.json")
        if not dia:
            avisos.append(f"no hay seguimiento del {fecha}")
            continue
        pred = predecir_dia(dia, args.cache, filas, x)
        _escribir_json(args.salida / f"{fecha}.json", pred)
        print(f"Predicción del {fecha}: {sum(c['parecido'] is not None for c in pred['monedas'])} de "
              f"{len(pred['monedas'])} monedas con huella")
    # Resultados a 10 días de las predicciones anteriores
    por_clave = {(f["mercado"], f["simbolo"], f["escaneo"]): f for f in filas}
    for ruta in sorted(args.salida.glob("*.json")):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", ruta.stem):
            continue
        pred = _leer_json(ruta)
        if pred and "monedas" in pred and completar_resultados(pred, por_clave):
            _escribir_json(ruta, pred)
    if args.probar:
        res = probar(filas, x)
        _escribir_json(args.salida / "prueba.json", res)
        for g in res["grupos"].values():
            print(f"Prueba {g['nombre']}: base {g['base']:.1%}; las 5 mejores de cada día llegaron "
                  f"{g['top5']['combinada']:.1%} (velas grandes {g['top5']['velas_grandes']:.1%})")
    for aviso in avisos:
        print(f"Aviso: {aviso}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
