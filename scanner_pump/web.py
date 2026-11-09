"""Versión web: la página del scanner, lista para escanear desde el navegador.

    python -m scanner_pump.web site                                   # escribe site/index.html
    python -m scanner_pump.web site --seguimiento datos/seguimiento   # y site/seguimiento.json con todos los días
    python -m scanner_pump.web site --bstocks                         # y site/bstocks.json (acciones tokenizadas)
    python -m scanner_pump.web site --listados datos/listados.json    # y site/listados.json (fechas de listado)
    python -m scanner_pump.web site --prediccion datos/prediccion     # y site/prediccion/ (pestaña Predicción)
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from .binance import SPOT, BinanceClient
from .listados import read_listings
from .prediccion import en_vivo, reconstruida, resumen_dia
from .report import render_page
from .scanner import is_tokenized_stock
from .seguimiento import collect_days


DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def stock_symbols(client: Optional[BinanceClient] = None) -> list[str]:
    """Pares spot que Binance etiqueta como acciones tokenizadas (bStocks)."""
    tags = (client or BinanceClient(SPOT)).asset_tags()
    if tags is None:
        raise RuntimeError("no se pudo consultar la lista de productos de Binance")
    return sorted(symbol for symbol, symbol_tags in tags.items() if is_tokenized_stock(symbol_tags))


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def write_predictions(source: Path, dest: Path, generated: str) -> None:
    """Un archivo por día y un índice con los días, la prueba hacia atrás y cómo acertaron los días ya cerrados."""
    days = []
    for path in sorted(source.glob("*.json")):
        if not DAY_RE.fullmatch(path.stem):
            continue
        try:
            day = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if day.get("version") != 2:  # de la versión anterior (monedas que ya habían explotado)
            continue
        days.append(day)
        dest.mkdir(parents=True, exist_ok=True)
        write_json(dest / path.name, day)
    if not days:
        print(f"Aviso: no hay predicciones en {source}.")
        return
    try:
        test = json.loads((source / "prueba.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        test = None
    # Por fecha, la más reciente primero. Las reconstruidas (calculadas días después, con los datos que
    # había ese día) se miden aparte de las que se calcularon el mismo día.
    days.sort(key=lambda d: d.get("escaneo_utc") or d["fecha"], reverse=True)
    index = {"generado": generated, "prueba": test,
             "en_vivo": {"reales": en_vivo([d for d in days if not reconstruida(d)]),
                         "reconstruidas": en_vivo([d for d in days if reconstruida(d)])},
             "dias": [{"fecha": d["fecha"], "escaneo_utc": d.get("escaneo_utc"), "reconstruida": reconstruida(d),
                       "evaluadas": d.get("evaluadas", 0), "monedas": len(d.get("monedas", [])),
                       "top5": resumen_dia(d), "base": d.get("base")} for d in days]}
    write_json(dest / "indice.json", index)
    print(f"{len(days)} días de predicción en {dest}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scanner_pump.web", description=__doc__.splitlines()[0])
    parser.add_argument("carpeta", nargs="?", default="site", help="carpeta de salida (por defecto: site)")
    parser.add_argument("--seguimiento", metavar="CARPETA",
                        help="unir los archivos de seguimiento de CARPETA en seguimiento.json")
    parser.add_argument("--bstocks", action="store_true", help="escribir también bstocks.json")
    parser.add_argument("--prediccion", metavar="CARPETA",
                        help="publicar las predicciones de CARPETA (datos/prediccion) en prediccion/")
    parser.add_argument("--listados", metavar="ARCHIVO",
                        help="publicar las fechas de listado de ARCHIVO (datos/listados.json) como listados.json")
    args = parser.parse_args(argv)

    out = Path(args.carpeta)
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(render_page(None), encoding="utf-8")
    print(f"Web escrita en {out / 'index.html'}")
    generated = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if args.seguimiento:
        days = collect_days(Path(args.seguimiento))
        write_json(out / "seguimiento.json", {"generado": generated, "dias": days})
        print(f"{len(days)} días de seguimiento en {out / 'seguimiento.json'}")
    if args.listados:
        pares = read_listings(Path(args.listados))
        if pares:
            write_json(out / "listados.json", {"generado": generated, "pares": pares})
            print(f"{len(pares)} fechas de listado en {out / 'listados.json'}")
        else:
            print(f"Aviso: {args.listados} no existe o está vacío; la web buscará las fechas de listado en Binance.")
    if args.prediccion:
        write_predictions(Path(args.prediccion), out / "prediccion", generated)
    if args.bstocks:
        try:
            symbols = stock_symbols()
        except RuntimeError as exc:
            print(f"Aviso: {exc}; la web avisará de que pueden aparecer acciones tokenizadas.")
            return 0
        write_json(out / "bstocks.json", {"generado": generated, "simbolos": symbols})
        print(f"{len(symbols)} acciones tokenizadas en {out / 'bstocks.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
