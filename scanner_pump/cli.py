"""Interfaz de línea de comandos del scanner."""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import webbrowser
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from .binance import MARKETS, BinanceClient, BinanceError
from .fechas import (ZONA_NOMBRE, analysis_label, last_closed_label, local_text, long_date, open_of,
                     short_date, utc_text)
from .report import build_payload, write_report
from .scanner import CHART_INTERVALS, MIN_PCT, SORT_KEYS, ScanConfig, ScanResult, explosion_to_dict, fetch_charts, scan_market
from .seguimiento import write_day

REPORTS_DIR = Path("reportes")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="scanner-pump",
        description=(
            "Scanner de Binance (spot y futuros): lista las criptomonedas cuya vela diaria subió un 10 %% o más "
            "de un mínimo a un máximo posterior (el mínimo tiene que ir antes que el máximo) o de apertura a cierre."
        ),
    )
    p.add_argument("-m", "--mercado", choices=["spot", "futures", "ambos"], default="ambos",
                   help="mercado a escanear (por defecto: ambos)")
    p.add_argument("-f", "--fecha", metavar="AAAA-MM-DD",
                   help=f"vela diaria a analizar, con la fecha con que Binance la muestra en {ZONA_NOMBRE} "
                        "(por defecto: la última vela completa)")
    p.add_argument("--min-pct", type=float, default=MIN_PCT, metavar="N",
                   help="subida mínima en %% (por defecto: 10)")
    p.add_argument("--min-volumen", type=float, default=0.0, metavar="N",
                   help="volumen mínimo de la vela, en USDT (ej. 1000000)")
    p.add_argument("--incluir-stables", action="store_true",
                   help="no descartar stablecoins/fiat como base (USDC, FDUSD, EUR...)")
    p.add_argument("--incluir-acciones", action="store_true",
                   help="no descartar las acciones tokenizadas de Binance (bStocks: AAPLB, NVDAB...)")
    p.add_argument("-o", "--orden", choices=list(SORT_KEYS), default="rango",
                   help="orden de la tabla: rango (mín→máx, por defecto), cuerpo (apertura→cierre), volumen o símbolo")
    p.add_argument("--top", type=int, metavar="N", help="mostrar solo las N primeras de cada mercado")
    p.add_argument("--seguimiento", metavar="CARPETA",
                   help="guardar las explosiones en CARPETA/AAAA-MM-DD.json (ej. datos/seguimiento)")
    p.add_argument("--csv", metavar="ARCHIVO", help="guardar los resultados en CSV")
    p.add_argument("--json", metavar="ARCHIVO", help="guardar los resultados en JSON (incluye las velas)")
    p.add_argument("--html", metavar="ARCHIVO",
                   help="ruta del informe con los gráficos (por defecto: reportes/scanner-pump_<fecha>.html)")
    p.add_argument("--sin-grafico", action="store_true", help="no generar el informe con los gráficos")
    p.add_argument("--no-abrir", action="store_true", help="generar el informe pero no abrirlo en el navegador")
    p.add_argument("--workers", type=int, default=8, help="descargas en paralelo (por defecto: 8)")
    p.add_argument("--spot-url", help="URL base de la API spot (por defecto: https://data-api.binance.vision)")
    p.add_argument("--futures-url", help="URL base de la API de futuros (por defecto: https://www.binance.com)")
    return p


def format_price(value: Optional[float]) -> str:
    if value is None:
        return "-"
    if value == 0:
        return "0"
    decimals = max(2, min(10, 4 - math.floor(math.log10(abs(value)))))
    return f"{value:,.{decimals}f}"


def format_volume(value: Optional[float]) -> str:
    if value is None:
        return "-"
    for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(value) >= limit:
            return f"{value / limit:.2f}{suffix}"
    return f"{value:.0f}"


def format_pct(value: Optional[float]) -> str:
    return "-" if value is None else f"{value:+.2f}%"


def render_table(headers: Sequence[str], rows: Sequence[Sequence[str]], right_align: set[int]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        widths = [max(w, len(cell)) for w, cell in zip(widths, row)]

    def line(cells: Sequence[str]) -> str:
        return "  ".join(
            cell.rjust(w) if i in right_align else cell.ljust(w)
            for i, (cell, w) in enumerate(zip(cells, widths))
        ).rstrip()

    out = [line(headers), line(["-" * w for w in widths])]
    out.extend(line(row) for row in rows)
    return "\n".join(out)


def render_result(result: ScanResult, order: str, top: Optional[int]) -> str:
    config = result.config
    explosions = sorted(result.explosions, key=SORT_KEYS[order])
    shown = explosions[:top] if top else explosions
    body_hits = sum(1 for e in explosions if e.pct_cuerpo >= config.min_pct)

    title = (
        f"{result.market.label.upper()} ({config.quote}) — {len(explosions)} velas ≥ {config.min_pct:g}% "
        f"de {result.analyzed} analizadas ({body_hits} también de apertura a cierre)"
    )
    notes = []
    if result.skipped_stocks:
        notes.append(f"{result.skipped_stocks} acciones tokenizadas excluidas")
    if result.skipped_volume:
        notes.append(f"{result.skipped_volume} descartadas por volumen")
    if result.max_before_min:
        notes.append(f"{result.max_before_min} descartadas porque el máximo fue antes del mínimo")
    if result.no_candle:
        notes.append(f"{result.no_candle} sin vela ese día")
    if result.errors:
        notes.append(f"{len(result.errors)} con error")
    if notes:
        title += f"\n  ({', '.join(notes)})"

    if not shown:
        return f"{title}\n  Ninguna vela subió {config.min_pct:g}% o más."

    headers = ["#", "Símbolo", "Mín→Máx", "Hora mín→máx (UTC)", "Apert→Cierre", "Apertura", "Cierre",
               "Día análisis", "Vol. vela"]
    rows = [
        [
            str(n),
            e.symbol,
            format_pct(e.pct_rango),
            f"{utc_hour(e.rise.low_time)}→{utc_hour(e.rise.high_time)}",
            format_pct(e.pct_cuerpo) + (" ✓" if e.pct_cuerpo >= config.min_pct else ""),
            format_price(e.candle.open),
            format_price(e.candle.close),
            format_pct(e.analysis.pct_cuerpo) if e.analysis else "-",
            format_volume(e.candle.quote_volume),
        ]
        for n, e in enumerate(shown, 1)
    ]
    table = render_table(headers, rows, right_align={0, 2, 4, 5, 6, 7, 8})
    footer = f"\n  … y {len(explosions) - len(shown)} más (quita --top para verlas todas)" if len(shown) < len(explosions) else ""
    return f"{title}\n{table}{footer}"


CSV_FIELDS = ["mercado", "simbolo", "base", "quote", "fecha", "fecha_analisis", "open", "high", "low", "close",
              "volumen", "pct_rango", "pct_cuerpo", "min_precio", "min_hora_utc", "max_precio", "max_hora_utc"]


def utc_hour(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%H:%M")


def export_csv(path: str, results: Sequence[ScanResult], order: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for r in results:
            for e in sorted(r.explosions, key=SORT_KEYS[order]):
                writer.writerow(dict(explosion_to_dict(e), min_hora_utc=utc_text(e.rise.low_time),
                                     max_hora_utc=utc_text(e.rise.high_time)))


def _progress_printer(label: str, unit: str = "pares descargados"):
    if not sys.stderr.isatty():
        return None

    def report(done: int, total: int) -> None:
        end = "\n" if done == total else ""
        print(f"\r  {label}: {done}/{total} {unit}", end=end, file=sys.stderr, flush=True)

    return report


def resolve_date(text: Optional[str], parser: argparse.ArgumentParser) -> date:
    last = last_closed_label()
    if not text:
        return last
    try:
        fecha = date.fromisoformat(text)
    except ValueError:
        parser.error("--fecha debe tener el formato AAAA-MM-DD")
    if fecha > last:
        parser.error(f"la vela del {long_date(fecha)} todavía no ha terminado; la última completa es la del "
                     f"{long_date(last)}")
    return fecha


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.workers < 1:
        parser.error("--workers debe ser al menos 1")
    if args.min_pct <= 0:
        parser.error("--min-pct debe ser positivo")

    fecha = resolve_date(args.fecha, parser)
    open_ms = open_of(fecha)
    config = ScanConfig(
        open_ms=open_ms,
        min_pct=args.min_pct,
        min_quote_volume=args.min_volumen,
        include_stables=args.incluir_stables,
        include_stocks=args.incluir_acciones,
        workers=args.workers,
    )
    market_keys = ["spot", "futures"] if args.mercado == "ambos" else [args.mercado]
    base_urls = {"spot": args.spot_url, "futures": args.futures_url}

    started = datetime.now(timezone.utc)
    print(
        f"Scanner-pump · velas 1D con subida ≥ {config.min_pct:g}% · vela del {long_date(fecha)} "
        f"(abre {local_text(open_ms)} {ZONA_NOMBRE} = {utc_text(open_ms)}) · "
        f"fecha de análisis {short_date(analysis_label(fecha))}\n"
    )

    results: list[ScanResult] = []
    failed = False
    for key in market_keys:
        market = MARKETS[key]
        client = BinanceClient(market, base_url=base_urls[key], pool_size=max(config.workers, 10))
        try:
            result = scan_market(client, config, progress=_progress_printer(market.label))
        except BinanceError as exc:
            failed = True
            print(f"{market.label.upper()}: error — {exc}\n", file=sys.stderr)
            continue
        results.append(result)
        if client.base_url != (base_urls[key] or market.base_url).rstrip("/"):
            print(f"  Nota: {market.base_url} rechazó la conexión; se usó {client.base_url}.", file=sys.stderr)
        print(render_result(result, args.orden, args.top) + "\n")
        if result.explosions and (not args.sin_grafico or args.json):
            fetch_charts(client, result, CHART_INTERVALS, progress=_progress_printer(market.label, "gráficos descargados"))
        for warning in result.warnings:
            print(f"  Aviso: {warning}", file=sys.stderr)
        for symbol, error in result.errors[:5]:
            print(f"  ! {symbol}: {error}", file=sys.stderr)

    if len(results) == 2:
        common = sorted({e.base for e in results[0].explosions} & {e.base for e in results[1].explosions})
        if common:
            print(f"En spot y futuros a la vez ({len(common)}): {', '.join(common)}\n")

    if not results:
        return 1

    if args.seguimiento:
        coins = [explosion_to_dict(e) for r in results for e in r.explosions]
        if coins:
            path = write_day(Path(args.seguimiento), fecha, coins)
            print(f"Seguimiento guardado en {path}")
        else:
            print("Ninguna explosión que guardar en el seguimiento.")

    payload = build_payload(results, started, args.orden)
    if args.csv:
        export_csv(args.csv, results, args.orden)
        print(f"Resultados guardados en {args.csv}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
        print(f"Resultados guardados en {args.json}")
    if not args.sin_grafico:
        path = write_report(Path(args.html) if args.html else REPORTS_DIR / f"scanner-pump_{fecha.isoformat()}.html", payload)
        print(f"Gráficos interactivos: {path}")
        if not args.no_abrir:
            webbrowser.open(path.resolve().as_uri())

    return 1 if failed else 0
