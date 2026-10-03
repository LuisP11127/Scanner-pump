"""Seguimiento diario: un archivo por vela de explosión, ``datos/seguimiento/AAAA-MM-DD.json``.

Cada archivo guarda las monedas cuya vela diaria de esa fecha subió un 10 % o más, con la fecha de
análisis (la vela anterior) y los datos de las dos velas::

    {
     "fecha": "2026-10-01",            # vela de la explosión (como la muestra Binance en hora Lima)
     "fecha_analisis": "2026-09-30",   # la vela anterior, la del análisis
     "apertura_utc": "2026-10-02T00:00:00Z",
     "monedas": [{"mercado": "spot", "simbolo": "XXXUSDT", "pct_rango": 23.4, ...}, ...]
    }

Los escriben el workflow «Escaneo diario» y la web (si se conecta con GitHub). El workflow «Web» los
une en ``seguimiento.json`` al publicar la página.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Sequence

from .fechas import analysis_label, open_of

DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
COIN_FIELDS = (
    "mercado", "simbolo", "base", "quote", "fecha", "fecha_analisis", "apertura",
    "open", "high", "low", "close", "volumen", "pct_rango", "pct_cuerpo", "analisis",
)


def coin_key(coin: dict) -> str:
    return f"{coin.get('mercado')}:{coin.get('simbolo')}"


def clean_coin(coin: dict) -> dict:
    return {f: coin.get(f) for f in COIN_FIELDS}


def make_day(fecha: date, coins: Iterable[dict]) -> dict:
    open_ms = open_of(fecha)
    return {
        "fecha": fecha.isoformat(),
        "fecha_analisis": analysis_label(fecha).isoformat(),
        "apertura_utc": datetime.fromtimestamp(open_ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "monedas": [clean_coin(c) for c in coins],
    }


def merge_days(old: Optional[dict], new: dict) -> dict:
    """Une dos versiones del mismo día: cada moneda (mercado + símbolo) una vez, con los datos más nuevos."""
    coins: dict[str, dict] = {}
    for day in (old, new):
        for c in (day or {}).get("monedas") or []:
            if isinstance(c, dict) and c.get("mercado") and c.get("simbolo"):
                coins[coin_key(c)] = clean_coin(c)
    merged = dict(new)
    merged["monedas"] = sorted(coins.values(), key=lambda c: -(c.get("pct_rango") or 0))
    return merged


def read_day(path: Path) -> Optional[dict]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("monedas"), list):
        return None
    return data


def write_day(folder: Path, fecha: date, coins: Iterable[dict]) -> Path:
    """Guarda (uniendo con lo que ya hubiera) las explosiones de la vela ``fecha``."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{fecha.isoformat()}.json"
    day = merge_days(read_day(path), make_day(fecha, coins))
    path.write_text(json.dumps(day, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return path


def collect_days(folder: Path) -> list[dict]:
    """Todos los días de seguimiento de la carpeta, del más reciente al más antiguo.

    Los archivos que no tengan el formato esperado se ignoran con un aviso.
    """
    days: dict[str, dict] = {}
    for path in sorted(Path(folder).glob("*.json")):
        data = read_day(path)
        if data is None:
            print(f"Aviso: {path} no tiene el formato de seguimiento; se ignora.")
            continue
        fecha = data.get("fecha")
        if not (isinstance(fecha, str) and DAY_RE.match(fecha)):
            fecha = path.stem if DAY_RE.match(path.stem) else None
        if not fecha:
            print(f"Aviso: {path} no indica la fecha; se ignora.")
            continue
        day = make_day(date.fromisoformat(fecha), [])
        day = merge_days(None, dict(day, monedas=data["monedas"]))
        if day["monedas"]:
            days[fecha] = day
    return [days[f] for f in sorted(days, reverse=True)]


def merge_folder(src: Path, dst: Path) -> list[Path]:
    """Une cada día de ``src`` con el mismo día de ``dst`` (lo usa el workflow tras traer lo último de GitHub)."""
    written = []
    for path in sorted(Path(src).glob("*.json")):
        data = read_day(path)
        fecha = data.get("fecha") if data else None
        if not (isinstance(fecha, str) and DAY_RE.match(fecha)):
            print(f"Aviso: {path} no tiene el formato de seguimiento; se ignora.")
            continue
        written.append(write_day(Path(dst), date.fromisoformat(fecha), data["monedas"]))
    return written


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scanner_pump.seguimiento",
        description="Une los días de seguimiento de ORIGEN con los de DESTINO (ej. datos/seguimiento).",
    )
    parser.add_argument("origen")
    parser.add_argument("destino")
    args = parser.parse_args(argv)
    for path in merge_folder(Path(args.origen), Path(args.destino)):
        print(f"Seguimiento actualizado: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
