"""Versión web: la página del scanner, lista para escanear desde el navegador.

    python -m scanner_pump.web site                                   # escribe site/index.html
    python -m scanner_pump.web site --seguimiento datos/seguimiento   # y site/seguimiento.json con todos los días
    python -m scanner_pump.web site --bstocks                         # y site/bstocks.json (acciones tokenizadas)
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from .binance import SPOT, BinanceClient
from .report import render_page
from .scanner import is_tokenized_stock
from .seguimiento import collect_days


def stock_symbols(client: Optional[BinanceClient] = None) -> list[str]:
    """Pares spot que Binance etiqueta como acciones tokenizadas (bStocks)."""
    tags = (client or BinanceClient(SPOT)).asset_tags()
    if tags is None:
        raise RuntimeError("no se pudo consultar la lista de productos de Binance")
    return sorted(symbol for symbol, symbol_tags in tags.items() if is_tokenized_stock(symbol_tags))


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scanner_pump.web", description=__doc__.splitlines()[0])
    parser.add_argument("carpeta", nargs="?", default="site", help="carpeta de salida (por defecto: site)")
    parser.add_argument("--seguimiento", metavar="CARPETA",
                        help="unir los archivos de seguimiento de CARPETA en seguimiento.json")
    parser.add_argument("--bstocks", action="store_true", help="escribir también bstocks.json")
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
