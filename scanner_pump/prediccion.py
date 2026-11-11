"""Predicción: monedas que aún no explotaron cuya vela de análisis se parece a la de las que después subieron.

**La idea.** En el seguimiento, la vela de análisis es la del día anterior a la explosión: la moneda todavía no
subió. Cada día, al cierre de la vela diaria (00:00 UTC = 19:00 hora Lima), se revisan **todas** las monedas de
Binance que aún no explotaron (su vela del día subió menos de 10 % de mínimo a máximo) y se buscan las que
se parecen a lo que tenían, en su vela de análisis, las que al día siguiente explotaron (vela de +10 %) y desde
ese momento subieron +50 %.

**La huella.** Las velas de 2H, 4H, 8H y 12H hasta el cierre, 84 de cada una (7, 14, 28 y 42 días); las de 4H, 8H y
12H se construyen con las de 2H, que empiezan a las 00:00 UTC igual que las de Binance. De cada temporalidad salen:

- la forma del precio (el cierre de cada tramo frente al último cierre, en 12 puntos);
- el volumen (cómo se reparte a lo largo del tramo, en 6 puntos);
- la volatilidad (rango medio de las velas) y la tendencia (variación del tramo);
- los **fractales** de 5 velas (un techo es una vela con el máximo más alto que las dos anteriores y las dos
  siguientes; un suelo, con el mínimo más bajo): cuántos hay, si los suelos y los techos van subiendo, la
  distancia del precio al último techo y al último suelo, y si ya rompió el último techo. Solo cuentan los
  fractales confirmados antes del cierre (los de las dos últimas velas aún no lo están).

**La biblioteca.** Todas las monedas cada 2 días desde 2023 con lo que pasó después: acertó si la vela siguiente
fue una explosión (+10 % de mínimo a máximo) y el máximo de los 10 días siguientes subió +50 % sobre el cierre de
la vela de análisis (el momento en que empieza la explosión). Se compara con todas, también con las que no
subieron: una huella que tienen muchas ganadoras pero también muchas que no subieron no puntúa alto.

**La puntuación.** De las 100 situaciones pasadas (ya con resultado) más parecidas, qué parte acertó.
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


# --- La rejilla: todas las monedas, cada 2 días desde 2023 ---------------------------------------------------

DESDE_REJILLA = 1672531200000  # 2023-01-01
PASO_REJILLA = 2 * DIA_MS
QUIETA = 10.0  # % de mínimo a máximo: con menos, la vela de análisis aún no es una explosión
EXPLOSION = 10.0  # % de mínimo a máximo de la vela siguiente para contar que explotó
VECINAS = 100
MUESTRA_NO = 4  # de las situaciones que no llegaron a +50 % la biblioteca guarda 1 de cada 4 (y pesan 4)
TOP = 30  # monedas que se guardan de cada predicción
VERSION = 3  # 3: el objetivo es explotar al día siguiente y subir +50 % desde ahí (2: solo +50 %)
HISTORIA_MIN = 45 * DIA_MS  # días de velas que necesita una moneda para tener huella
MOVIMIENTO_MIN = 5.0  # % de mínimo a máximo en los últimos 30 días: con menos es casi una moneda estable


def se_mueve(t: np.ndarray, v: np.ndarray, dia: int) -> bool:
    """¿Se movió el precio en los 30 días antes de ``dia``? (descarta monedas estables o sin actividad)"""
    mes = v[int(np.searchsorted(t, dia - 30 * DIA_MS)):int(np.searchsorted(t, dia))]
    return len(mes) > 0 and float(mes[:, 2].min()) > 0 and (float(mes[:, 1].max()) / float(mes[:, 2].min()) - 1) * 100 >= MOVIMIENTO_MIN


def vela_analisis(t: np.ndarray, v: np.ndarray, dia: int) -> Optional[dict]:
    """La vela diaria que cierra en ``dia`` (00:00 UTC) hecha con las de 2H, y lo que pasó después si ya se sabe:
    ``manana`` (de mínimo a máximo de la vela siguiente) y ``max_despues`` (máximo de los 10 días siguientes frente
    al cierre). ``None`` si el par no cotizaba justo antes del cierre."""
    i = int(np.searchsorted(t, dia))
    if i == 0 or int(t[i - 1]) != dia - PASO_2H:
        return None
    hoy = v[int(np.searchsorted(t, dia - DIA_MS)):i]
    if len(hoy) < 6:
        return None
    o, c, hi, lo = float(hoy[0, 0]), float(hoy[-1, 3]), float(hoy[:, 1].max()), float(hoy[:, 2].min())
    if min(o, c, lo) <= 0:
        return None
    fila = {"apertura": dia - DIA_MS, "open": o, "high": hi, "low": lo, "close": c,
            "volumen": round(float((hoy[:, 4] * hoy[:, 3]).sum()), 2),
            "pct_rango": round((hi / lo - 1) * 100, 2), "pct_cuerpo": round((c / o - 1) * 100, 2),
            "manana": None, "max_despues": None}
    ultimo = int(t[-1]) + PASO_2H  # cierre de la última vela de la caché
    if ultimo >= dia + DIA_MS:
        m = v[i:int(np.searchsorted(t, dia + DIA_MS))]
        if len(m):
            fila["manana"] = round((float(m[:, 1].max()) / float(m[:, 2].min()) - 1) * 100, 2)
    if ultimo >= dia + VENTANA_DIAS * DIA_MS:
        f = v[i:int(np.searchsorted(t, dia + VENTANA_DIAS * DIA_MS))]
        if len(f):
            fila["max_despues"] = round((float(f[:, 1].max()) / c - 1) * 100, 2)
    return fila


def acierto(manana, max_despues):
    """El objetivo: la vela siguiente es la explosión (+10 % de mínimo a máximo) y desde que empieza (el cierre de la
    vela de análisis) el precio sube +50 % en los 10 días siguientes."""
    with np.errstate(invalid="ignore"):
        return (np.asarray(manana, float) >= EXPLOSION) & (np.asarray(max_despues, float) >= OBJETIVO)


def _muestra(simbolo: str, dia: int) -> bool:
    """¿Entra en la biblioteca una situación que no llegó a +50 %? (1 de cada ``MUESTRA_NO``, siempre la misma)."""
    return (int(dia // DIA_MS) + sum(map(ord, simbolo))) % MUESTRA_NO == 0


class Rejilla:
    """Cada par cada 2 días desde 2023 (al cierre de la vela): su huella y lo que pasó después."""

    def __init__(self, pares: list[str], par: np.ndarray, dia: np.ndarray, rango: np.ndarray, manana: np.ndarray,
                 max10: np.ndarray, x: np.ndarray) -> None:
        self.pares, self.par, self.dia, self.rango, self.manana, self.max10, self.x = pares, par, dia, rango, manana, max10, x

    def __len__(self) -> int:
        return len(self.dia)

    def biblioteca(self, hasta: int) -> "Biblioteca":
        """Las situaciones cuyo resultado a 10 días ya se conocía en ``hasta`` (las que no subieron, muestreadas)."""
        llego = acierto(self.manana, self.max10)
        conocida = np.isfinite(self.max10) & np.isfinite(self.manana) & (self.dia + VENTANA_DIAS * DIA_MS <= hasta)
        muestra = np.array([_muestra(self.pares[p].split("/")[1], int(d)) for p, d in zip(self.par, self.dia)], bool)
        sel = np.flatnonzero(conocida & (llego | muestra))
        return Biblioteca(self.x[sel], llego[sel], np.where(llego[sel], 1.0, float(MUESTRA_NO)),
                          [(self.pares[self.par[i]], int(self.dia[i]), float(self.max10[i])) for i in sel])


def _rejilla_de(ruta: Path) -> tuple[list, list]:
    t, v = leer_cache(ruta)
    v = v.astype(np.float64)
    if len(t) < 400:
        return [], []
    grupos = agrupadas(t, v)
    inicio = max(DESDE_REJILLA, (int(t[0]) // DIA_MS) * DIA_MS + HISTORIA_MIN)
    inicio += (inicio - DESDE_REJILLA) % PASO_REJILLA
    filas, xs = [], []
    for dia in range(inicio, int(t[-1]) + PASO_2H + 1, PASO_REJILLA):
        a = vela_analisis(t, v, dia)
        if a is None or not se_mueve(t, v, dia):
            continue
        x = huella(t, v, dia, grupos)
        if x is None:
            continue
        filas.append((dia, a["pct_rango"], np.nan if a["manana"] is None else a["manana"],
                      np.nan if a["max_despues"] is None else a["max_despues"]))
        xs.append(x)
    return filas, xs


def construir_rejilla(carpeta: Path) -> Rejilla:
    rutas = sorted(carpeta.glob("*/*.npz"))
    pares, par, filas, xs = [], [], [], []
    with ProcessPoolExecutor() as pool:
        for ruta, (f, x) in zip(rutas, pool.map(_rejilla_de, rutas, chunksize=8)):
            if f:
                par += [len(pares)] * len(f)
                pares.append(f"{ruta.parent.name}/{ruta.stem}")
                filas += f
                xs += x
    a = np.array(filas, dtype=np.float64).reshape(-1, 4)
    return Rejilla(pares, np.array(par, np.int32), a[:, 0].astype(np.int64), a[:, 1], a[:, 2], a[:, 3],
                   np.vstack(xs).astype(np.float32) if xs else np.zeros((0, len(nombres())), np.float32))


class Biblioteca:
    """Situaciones pasadas (huella al cierre de una vela) con su resultado: ¿explotó y subió +50 %?"""

    def __init__(self, x: np.ndarray, llego: np.ndarray, peso: Optional[np.ndarray] = None, info: Optional[list] = None) -> None:
        self.llego = np.asarray(llego, bool)
        self.peso = np.ones(len(self.llego)) if peso is None else np.asarray(peso, float)
        self.info = info or []
        self.media = x.mean(axis=0) if len(x) else 0
        self.escala = x.std(axis=0) + 1e-6 if len(x) else 1
        self.x = ((x - self.media) / self.escala).astype(np.float32)
        self.norma = (self.x ** 2).sum(axis=1)

    def __len__(self) -> int:
        return len(self.llego)

    def vecinas(self, consultas: np.ndarray, k: Optional[int] = None) -> np.ndarray:
        """Índices de las ``k`` más parecidas a cada consulta, de la más a la menos parecida."""
        k = min(k or VECINAS, len(self))
        q = ((consultas - self.media) / self.escala).astype(np.float32)
        out = np.empty((len(q), k), dtype=np.int64)
        for a in range(0, len(q), 256):
            d = self.norma[None, :] - 2 * q[a:a + 256] @ self.x.T
            idx = np.argpartition(d, k - 1, axis=1)[:, :k] if k < len(self) else np.tile(np.arange(len(self)), (len(d), 1))
            orden = np.take_along_axis(d, idx, axis=1).argsort(axis=1)
            out[a:a + 256] = np.take_along_axis(idx, orden, axis=1)
        return out

    def puntuar(self, consultas: np.ndarray, k: Optional[int] = None) -> tuple[np.ndarray, np.ndarray]:
        """Qué parte de las parecidas acertó (con su peso: cuentan también las que no)."""
        idx = self.vecinas(consultas, k)
        p = self.peso[idx]
        return (self.llego[idx] * p).sum(axis=1) / p.sum(axis=1), idx


# --- Predicción del día ----------------------------------------------------------------------------------------

def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def etiqueta(dia: int) -> str:
    """Fecha (hora Lima) con la que Binance muestra la vela de análisis que cierra en ``dia``."""
    from .fechas import label_of
    return label_of(dia - DIA_MS).isoformat()


def candidatas(carpeta: Path, dia: int) -> tuple[list[dict], np.ndarray]:
    """Las monedas cuya vela de análisis (la que cierra en ``dia``) aún no es una explosión, con su huella."""
    monedas, xs = [], []
    for ruta in sorted(carpeta.glob("*/*.npz")):
        t, v = leer_cache(ruta)
        if len(t) < 400 or int(t[0]) > dia - HISTORIA_MIN:
            continue
        v = v.astype(np.float64)
        a = vela_analisis(t, v, dia)
        if a is None or a["pct_rango"] >= QUIETA or not se_mueve(t, v, dia):
            continue
        x = huella(t, v, dia)
        if x is None:
            continue
        simbolo = ruta.stem
        monedas.append({"mercado": ruta.parent.name, "simbolo": simbolo, "base": simbolo[:-4], "quote": simbolo[-4:],
                        "fecha": etiqueta(dia), **a})
        xs.append(x)
    return monedas, (np.vstack(xs) if xs else np.zeros((0, len(nombres())), np.float32))


def predecir(carpeta: Path, dia: int, rejilla: Rejilla, ahora: Optional[int] = None) -> dict:
    """Las ``TOP`` monedas que aún no explotaron cuya huella al cierre de ``dia`` más se parece a la que tenían, en
    su vela de análisis, las que al día siguiente explotaron y desde ahí subieron +50 %. Solo usa lo que se sabía."""
    biblio = rejilla.biblioteca(dia)
    monedas, x = candidatas(carpeta, dia)
    out = {"version": VERSION, "fecha": etiqueta(dia), "escaneo_utc": _iso(dia),
           "calculada": _iso(ahora or int(datetime.now(timezone.utc).timestamp() * 1000)),
           "biblioteca": {"situaciones": len(biblio), "llegaron": int(biblio.llego.sum())},
           "evaluadas": 0, "monedas": []}
    if not monedas or not len(biblio):
        return out
    parecido, idx = biblio.puntuar(x)
    mejor = {}
    for i, c in enumerate(monedas):  # la misma moneda en spot y en futuros: se queda la que más se parece
        if c["base"] not in mejor or parecido[i] > parecido[mejor[c["base"]]]:
            mejor[c["base"]] = i
    elegidas = sorted(mejor.values(), key=lambda i: -parecido[i])
    out["evaluadas"] = len(mejor)
    for i in elegidas[:TOP]:
        c = monedas[i]
        ganadoras = [j for j in idx[i] if biblio.llego[j]][:3] or list(idx[i][:3])
        c["parecido"] = round(float(parecido[i]), 4)
        c["vecinas"] = [[*biblio.info[j][0].split("/")[::-1], biblio.info[j][1], round(biblio.info[j][2], 2)] for j in ganadoras]
        out["monedas"].append(c)
    return out


def completar(prediccion: dict, carpeta: Path) -> bool:
    """Añade lo que ya se sabe de cada moneda (vela siguiente y máximo a 10 días) y, cuando se sabe, la media de
    todas las monedas que no habían explotado ese día. Devuelve si cambió algo."""
    dia = int(datetime.fromisoformat(prediccion["escaneo_utc"].replace("Z", "+00:00")).timestamp() * 1000)
    cambio = False
    for c in prediccion.get("monedas", []):
        if c.get("max_despues") is not None:
            continue
        t, v = leer_cache(carpeta / c["mercado"] / f"{c['simbolo']}.npz")
        a = vela_analisis(t, v.astype(np.float64), dia) if len(t) else None
        if a:
            for k in ("manana", "max_despues"):
                if a[k] is not None and c.get(k) != a[k]:
                    c[k] = a[k]
                    cambio = True
    base = prediccion.get("base") or {}
    if base.get("llegaron") is None:
        nueva = media_del_dia(carpeta, dia)
        if nueva and nueva != base:
            prediccion["base"] = nueva
            cambio = True
    return cambio


def media_del_dia(carpeta: Path, dia: int) -> Optional[dict]:
    """De todas las monedas que no habían explotado ese día: cuántas explotaron al día siguiente, cuántas llegaron
    a +50 % en 10 días y cuántas las dos cosas (``None`` hasta que haya pasado el día siguiente)."""
    manana, llegaron, acertaron, vistas = [], [], [], set()
    for ruta in sorted(carpeta.glob("*/*.npz"), key=lambda r: (r.parent.name != "futures", r.stem)):
        if ruta.stem[:-4] in vistas:  # cada moneda una vez aunque esté en spot y en futuros
            continue
        t, v = leer_cache(ruta)
        if len(t) < 400 or int(t[0]) > dia - HISTORIA_MIN:
            continue
        a = vela_analisis(t, v.astype(np.float64), dia)
        if a is None or a["pct_rango"] >= QUIETA or not se_mueve(t, v, dia):
            continue
        vistas.add(ruta.stem[:-4])
        if a["manana"] is not None:
            manana.append(a["manana"] >= EXPLOSION)
        if a["max_despues"] is not None:
            llegaron.append(a["max_despues"] >= OBJETIVO)
            acertaron.append(bool(acierto(a["manana"], a["max_despues"])))
    if not manana:
        return None
    return {"monedas": len(manana), "explotaron": round(float(np.mean(manana)), 4),
            "llegaron": round(float(np.mean(llegaron)), 4) if len(llegaron) >= len(manana) * 0.9 else None,
            "acertaron": round(float(np.mean(acertaron)), 4) if len(acertaron) >= len(manana) * 0.9 else None}


def reconstruida(prediccion: dict) -> bool:
    """¿Se calculó días después del cierre de la vela (con lo que se sabía ese día) y no el mismo día?"""
    calculada, escaneo = prediccion.get("calculada"), prediccion.get("escaneo_utc")
    if not calculada or not escaneo:
        return True
    a = datetime.fromisoformat(calculada.replace("Z", "+00:00"))
    return a - datetime.fromisoformat(escaneo.replace("Z", "+00:00")) > timedelta(days=1)


def resumen_dia(prediccion: dict, n: int = 5) -> dict:
    """De las ``n`` primeras: cuántas explotaron al día siguiente, cuántas llegaron a +50 % y cuántas acertaron (las
    dos cosas), de las que ya se sabe."""
    top = prediccion.get("monedas", [])[:n]
    man = [c["manana"] >= EXPLOSION for c in top if c.get("manana") is not None]
    con = [c for c in top if c.get("max_despues") is not None and c.get("manana") is not None]
    return {"explotaron": sum(man), "de_manana": len(man), "llegaron": sum(c["max_despues"] >= OBJETIVO for c in con),
            "acertaron": int(sum(acierto(c["manana"], c["max_despues"]) for c in con)), "de_10d": len(con)}


def en_vivo(predicciones: list[dict]) -> dict:
    """Acierto acumulado de las 5 primeras de cada predicción, frente a la media de las monedas de esos días."""
    r = {"dias_manana": 0, "explotaron": 0, "de_manana": 0, "base_explotaron": [],
         "dias_10d": 0, "llegaron": 0, "acertaron": 0, "de_10d": 0, "base_llegaron": [], "base_acertaron": []}
    for p in predicciones:
        s, base = resumen_dia(p), p.get("base") or {}
        if s["de_manana"]:
            r["dias_manana"] += 1
            r["explotaron"] += s["explotaron"]
            r["de_manana"] += s["de_manana"]
            if base.get("explotaron") is not None:
                r["base_explotaron"].append(base["explotaron"])
        if s["de_10d"] >= 5:
            r["dias_10d"] += 1
            r["llegaron"] += s["llegaron"]
            r["acertaron"] += s["acertaron"]
            r["de_10d"] += s["de_10d"]
            if base.get("llegaron") is not None:
                r["base_llegaron"].append(base["llegaron"])
            if base.get("acertaron") is not None:
                r["base_acertaron"].append(base["acertaron"])
    for k in ("base_explotaron", "base_llegaron", "base_acertaron"):
        r[k] = round(float(np.mean(r[k])), 4) if r[k] else None
    return r


# --- Prueba hacia atrás ----------------------------------------------------------------------------------

def probar(rejilla: Rejilla, desde: str = "2025-01-01") -> dict:
    """Mes a mes, sin mirar el futuro: la biblioteca solo tiene las situaciones cuyo resultado se conocía al empezar
    el mes. Cada día de la rejilla se eligen las 5 monedas (que aún no explotaron) más parecidas a las ganadoras."""
    inicio = int(datetime.fromisoformat(desde).replace(tzinfo=timezone.utc).timestamp() * 1000)
    meses = sorted({datetime.fromtimestamp(d / 1000, timezone.utc).strftime("%Y-%m") for d in rejilla.dia if d >= inicio})
    conocida = np.isfinite(rejilla.max10) & np.isfinite(rejilla.manana) & (rejilla.rango < QUIETA)
    bases = np.array([p.split("/")[1][:-4] for p in rejilla.pares])
    top5_lle, top5_exp, top5_ace, base_lle, base_exp, base_ace = [], [], [], [], [], []
    ok = acierto(rejilla.manana, rejilla.max10)
    for mes in meses:
        a = int(datetime.fromisoformat(mes + "-01").replace(tzinfo=timezone.utc).timestamp() * 1000)
        b = int(datetime.fromisoformat((datetime.fromisoformat(mes + "-01") + timedelta(days=32)).strftime("%Y-%m-01"))
                .replace(tzinfo=timezone.utc).timestamp() * 1000)
        prueba = np.flatnonzero(conocida & (rejilla.dia >= a) & (rejilla.dia < b))
        biblio = rejilla.biblioteca(a)
        if not len(prueba) or not len(biblio):
            continue
        score, _ = biblio.puntuar(rejilla.x[prueba])
        for dia in np.unique(rejilla.dia[prueba]):
            sel = np.flatnonzero(rejilla.dia[prueba] == dia)
            vistos, top = set(), []
            for j in sel[np.argsort(-score[sel], kind="stable")]:
                base = bases[rejilla.par[prueba[j]]]
                if base not in vistos:
                    vistos.add(base)
                    top.append(prueba[j])
                if len(top) == 5:
                    break
            filas = prueba[sel]
            top5_lle.append(np.mean(rejilla.max10[top] >= OBJETIVO))
            top5_exp.append(np.mean(rejilla.manana[top] >= EXPLOSION))
            base_lle.append(np.mean(rejilla.max10[filas] >= OBJETIVO))
            base_exp.append(np.mean(rejilla.manana[filas] >= EXPLOSION))
            top5_ace.append(np.mean(ok[top]))
            base_ace.append(np.mean(ok[filas]))
    r = lambda xs: round(float(np.mean(xs)), 4) if xs else None
    return {"desde": desde, "dias": len(top5_lle), "situaciones": int(conocida[rejilla.dia >= inicio].sum()),
            "base": {"llegaron": r(base_lle), "explotaron": r(base_exp), "acertaron": r(base_ace)},
            "top5": {"llegaron": r(top5_lle), "explotaron": r(top5_exp), "acertaron": r(top5_ace)}}


# --- Línea de comandos -----------------------------------------------------------------------------------

def _leer_json(ruta: Path) -> Optional[dict]:
    try:
        return json.loads(ruta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _escribir_json(ruta: Path, datos: dict) -> None:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(json.dumps(datos, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")


def _dia_de(fecha: str) -> int:
    """Cierre (00:00 UTC) de la vela de análisis con esa fecha (hora Lima)."""
    from .fechas import open_of
    return open_of(datetime.fromisoformat(fecha).date()) + DIA_MS


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scanner_pump.prediccion", description=__doc__.splitlines()[0])
    parser.add_argument("--salida", type=Path, default=Path("datos/prediccion"))
    parser.add_argument("--cache", type=Path, default=Path("cache/velas-2h"), help="velas de 2H (no van al repositorio)")
    parser.add_argument("--fecha", nargs="*", default=[],
                        help="velas de análisis a predecir (AAAA-MM-DD, hora Lima; por defecto la última y las que falten)")
    parser.add_argument("--probar", action="store_true", help="repetir la prueba hacia atrás (prueba.json)")
    parser.add_argument("--sin-actualizar", action="store_true", help="no pedir velas nuevas a Binance")
    args = parser.parse_args(argv)

    ahora = int(datetime.now(timezone.utc).timestamp() * 1000)
    avisos = [] if args.sin_actualizar else actualizar_cache(args.cache, ahora)
    rejilla = construir_rejilla(args.cache)
    print(f"Rejilla: {len(rejilla)} situaciones de {len(rejilla.pares)} pares desde 2023", file=sys.stderr)
    # Las predicciones de antes (de otra versión) se descartan
    hechas = {}
    for ruta in sorted(args.salida.glob("*.json")):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", ruta.stem):
            p = _leer_json(ruta)
            if not p or p.get("version") != VERSION:
                ruta.unlink()
            else:
                hechas[ruta.stem] = p
    ultimo = (ahora // DIA_MS) * DIA_MS  # cierre de la última vela diaria
    if args.fecha:
        dias = [_dia_de(f) for f in args.fecha]
    else:  # la última y las que falten de los 5 días anteriores (si el workflow no corrió)
        dias = [d for d in range(ultimo - 5 * DIA_MS, ultimo + 1, DIA_MS) if etiqueta(d) not in hechas]
    for dia in dias:
        pred = predecir(args.cache, dia, rejilla, ahora)
        completar(pred, args.cache)
        _escribir_json(args.salida / f"{pred['fecha']}.json", pred)
        hechas[pred["fecha"]] = pred
        print(f"Predicción del {_iso(dia)[:10]} (vela de análisis del {pred['fecha']}): {pred['evaluadas']} monedas sin "
              f"explotar; primeras: {', '.join(c['simbolo'] for c in pred['monedas'][:5])}")
    for fecha, pred in hechas.items():  # lo que ya se sabe de las anteriores
        if completar(pred, args.cache):
            _escribir_json(args.salida / f"{fecha}.json", pred)
    if args.probar:
        res = probar(rejilla)
        _escribir_json(args.salida / "prueba.json", res)
        print(f"Prueba desde {res['desde']}: de las 5 primeras de cada día explotaron y subieron +{OBJETIVO:.0f} % el "
              f"{res['top5']['acertaron']:.1%} (media {res['base']['acertaron']:.1%}); llegaron a +{OBJETIVO:.0f} % el "
              f"{res['top5']['llegaron']:.1%} (media {res['base']['llegaron']:.1%}); explotaron al día siguiente el "
              f"{res['top5']['explotaron']:.1%} (media {res['base']['explotaron']:.1%})")
    for aviso in avisos:
        print(f"Aviso: {aviso}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
