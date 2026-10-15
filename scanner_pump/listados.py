"""Monedas listadas en Binance: fecha de listado de cada par y precio inicial de las listadas desde 2026.

- Futuros: ``exchangeInfo`` trae la fecha de lanzamiento de cada contrato (``onboardDate``).
- Spot: ``exchangeInfo`` no la trae; es la apertura de la primera vela de 1 minuto del par.

La fecha de listado no cambia, así que se guarda en ``datos/listados.json`` y en cada ejecución solo se piden
los pares nuevos. El escaneo diario lo actualiza y la web lo usa en la pestaña «Monedas listadas 2026»::

    {
     "desde": "2026-01-01",
     "pares": {
      "spot:BTCUSDT": {"base": "BTC", "listado": 1502942400000},
      "futures:XUSDT": {"base": "X", "listado": 1760083200000, "precio_inicial": 0.45},
      ...
     }
    }

``listado`` es la hora de listado (ms UTC). ``precio_inicial`` (apertura de la primera vela de 1 minuto) solo se
guarda para los pares listados desde ``desde`` (1 de enero de 2026 en hora Lima). El archivo no lleva la hora en
que se generó: así solo cambia (y solo se guarda en el repositorio) cuando hay pares nuevos.

    python -m scanner_pump.listados datos/listados.json                       # actualiza el archivo
    python -m scanner_pump.listados datos/listados.json --salida otro.json   # lo deja en otro archivo
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Optional, Sequence

from .binance import MARKETS, BinanceClient, BinanceError
from .fechas import ZONA, now_ms
from .scanner import NON_CRYPTO_UNDERLYING, STABLECOINS, is_tokenized_stock

DESDE = date(2026, 1, 1)
# Pares que todavía no han empezado a cotizar.
PENDING_STATUS = frozenset({"PRE_TRADING", "PENDING_TRADING"})


def desde_ms(desde: date = DESDE) -> int:
    """Medianoche del día ``desde`` en hora Lima, en ms UTC."""
    return int(datetime(desde.year, desde.month, desde.day, tzinfo=ZONA).timestamp() * 1000)


def listing_symbols(market_key: str, exchange_info: dict, quote: str = "USDT") -> list[dict]:
    """Pares del exchangeInfo que cuentan como monedas: los mismos filtros que el escáner, pero también los que
    ya no cotizan (una moneda listada en 2026 y retirada después sigue siendo una moneda listada en 2026)."""
    selected = []
    for s in exchange_info.get("symbols", []):
        if s.get("status") in PENDING_STATUS or s.get("quoteAsset") != quote:
            continue
        if s.get("baseAsset") in STABLECOINS:
            continue
        if market_key == "spot":
            if not s.get("isSpotTradingAllowed", True):
                continue
        elif s.get("contractType") != "PERPETUAL" or s.get("underlyingType") in NON_CRYPTO_UNDERLYING:
            continue
        onboard = s.get("onboardDate") if market_key == "futures" else None
        selected.append({"symbol": s["symbol"], "base": s["baseAsset"], "onboard": int(onboard) if onboard else None})
    return selected


def _parallel(workers: int, func: Callable, items: Sequence, progress=None) -> list[tuple]:
    """[(item, resultado o BinanceError)] ejecutando ``func`` en paralelo."""
    out = []
    pool = ThreadPoolExecutor(max_workers=max(1, workers))
    try:
        jobs = {pool.submit(func, item): item for item in items}
        for done, job in enumerate(as_completed(jobs), 1):
            if progress:
                progress(done, len(jobs))
            try:
                out.append((jobs[job], job.result()))
            except BinanceError as exc:
                out.append((jobs[job], exc))
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    return out


def update_listings(
    clients: dict,
    cache: Optional[dict] = None,
    desde: date = DESDE,
    now: Optional[int] = None,
    workers: int = 8,
    progress: Optional[Callable[[int, int], None]] = None,
) -> tuple[dict, list[str], int]:
    """Fecha de listado de todos los pares de ``clients`` ({"spot": cliente, "futures": cliente}).

    Parte de ``cache`` (lo que ya se sabía) y solo consulta a Binance lo que falta. Devuelve (pares, avisos,
    velas pedidas). Los pares que ya no aparecen en Binance se conservan.
    """
    now = now_ms() if now is None else now
    since = desde_ms(desde)
    pares = {k: dict(v) for k, v in (cache or {}).items() if isinstance(v, dict) and v.get("listado")}
    warnings: list[str] = []
    jobs = []  # (clave, cliente, símbolo, startTime de la primera vela)
    for key, client in clients.items():
        try:
            info = client.exchange_info()
        except BinanceError as exc:
            warnings.append(f"{client.market.label}: {exc}")
            continue
        symbols = listing_symbols(key, info)
        if key == "spot":
            tags = client.asset_tags()
            if tags is None:
                warnings.append("no se pudo saber qué pares spot son acciones tokenizadas (bStocks)")
            else:
                symbols = [s for s in symbols if not is_tokenized_stock(tags.get(s["symbol"], ()))]
        for s in symbols:
            k = f"{key}:{s['symbol']}"
            entry = pares.setdefault(k, {})
            entry["base"] = s["base"]
            if s["onboard"]:
                entry["listado"] = s["onboard"]
            listado = entry.get("listado")
            if listado is None:
                jobs.append((k, client, s["symbol"], 0))  # la primera vela da la fecha y el precio inicial
            elif since <= listado <= now and entry.get("precio_inicial") is None:
                jobs.append((k, client, s["symbol"], listado))

    results = _parallel(workers, lambda job: job[1].klines(job[2], "1m", 1, start_time=job[3]), jobs, progress)
    failed = 0
    for (k, _client, _symbol, _start), rows in results:
        if isinstance(rows, BinanceError) or not rows:
            failed += 1  # se volverá a intentar en la próxima ejecución
            continue
        entry = pares[k]
        if entry.get("listado") is None:
            entry["listado"] = int(rows[0][0])
        if entry["listado"] >= since:
            entry["precio_inicial"] = float(rows[0][1])
    if failed:
        warnings.append(f"{failed} pares no respondieron; se reintentarán la próxima vez")
    pares = {k: v for k, v in sorted(pares.items()) if v.get("listado")}
    return pares, warnings, len(jobs)


def read_listings(path: Path) -> dict:
    """Los pares de un ``listados.json`` (vacío si no existe o no tiene el formato)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    pares = data.get("pares") if isinstance(data, dict) else None
    return pares if isinstance(pares, dict) else {}


def write_listings(path: Path, pares: dict, desde: date = DESDE) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Un par por línea: los cambios de cada día se leen bien en el historial del repositorio.
    lines = [f"  {json.dumps(k)}: {json.dumps(v, ensure_ascii=False, separators=(',', ':'))}" for k, v in pares.items()]
    body = ",\n".join(lines) + ("\n" if lines else "")
    path.write_text(f'{{\n "desde": {json.dumps(desde.isoformat())},\n "pares": {{\n{body} }}\n}}\n', encoding="utf-8")
    return path


def count_listed(pares: dict, desde: date = DESDE, now: Optional[int] = None) -> dict[str, int]:
    """Pares listados desde ``desde`` por mercado."""
    since, now = desde_ms(desde), now_ms() if now is None else now
    counts = {"spot": 0, "futures": 0}
    for k, v in pares.items():
        if since <= v.get("listado", 0) <= now:
            market = k.split(":", 1)[0]
            counts[market] = counts.get(market, 0) + 1
    return counts


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scanner_pump.listados", description=__doc__.splitlines()[0])
    parser.add_argument("archivo", help="listados.json con lo que ya se sabe (si no existe, se empieza de cero)")
    parser.add_argument("--salida", metavar="ARCHIVO", help="dónde escribir el resultado (por defecto: el mismo archivo)")
    parser.add_argument("--workers", type=int, default=8, help="descargas en paralelo (por defecto: 8)")
    parser.add_argument("--spot-url", help="URL base de la API spot")
    parser.add_argument("--futures-url", help="URL base de la API de futuros")
    args = parser.parse_args(argv)

    cache = read_listings(Path(args.archivo))
    clients = {
        "spot": BinanceClient(MARKETS["spot"], base_url=args.spot_url),
        "futures": BinanceClient(MARKETS["futures"], base_url=args.futures_url),
    }
    pares, warnings, asked = update_listings(clients, cache, workers=args.workers)
    for warning in warnings:
        print(f"Aviso: {warning}", file=sys.stderr)
    if not pares:
        print("No se pudo obtener ningún par de Binance.", file=sys.stderr)
        return 1
    path = write_listings(Path(args.salida or args.archivo), pares)
    counts = count_listed(pares)
    print(f"Monedas listadas desde el {DESDE.isoformat()}: spot {counts['spot']}, futuros {counts['futures']} "
          f"({len(pares)} pares en total, {asked} consultados ahora) → {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
