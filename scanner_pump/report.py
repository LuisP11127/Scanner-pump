"""Informe HTML interactivo (la misma página de la web) con los resultados de un escaneo."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence

from .fechas import ZONA_NOMBRE, analysis_label, label_of
from .scanner import CHART_INTERVALS, SORT_KEYS, ScanResult, explosion_to_dict

TEMPLATE_PATH = Path(__file__).with_name("plantilla.html")
DATA_MARKER = "__SCANNER_DATA__"


def build_payload(results: Sequence[ScanResult], generated_at: datetime, order: str = "rango") -> dict:
    """Resultados del escaneo con las velas de cada moneda: lo que leen el informe y el .json."""
    config = results[0].config
    fecha = label_of(config.open_ms)
    mercados = {}
    for r in results:
        coins = []
        for e in sorted(r.explosions, key=SORT_KEYS[order]):
            coin = explosion_to_dict(e)
            charts = r.charts.get(e.symbol)
            if charts:
                coin["graficos"] = charts
            coins.append(coin)
        mercados[r.market.key] = {
            "nombre": r.market.label,
            "quote": config.quote,
            "analizadas": r.analyzed,
            "coincidencias": coins,
        }
    return {
        "generado": generated_at.isoformat(timespec="seconds"),
        "fecha": fecha.isoformat(),
        "fecha_analisis": analysis_label(fecha).isoformat(),
        "apertura": config.open_ms,
        "min_pct": config.min_pct,
        "zona": ZONA_NOMBRE,
        "intervalos_grafico": list(CHART_INTERVALS),
        "orden": order,
        "mercados": mercados,
    }


def render_page(payload: Optional[dict]) -> str:
    """La página con los resultados de ``payload`` (o vacía, lista para escanear desde el navegador)."""
    # "<" solo aparece dentro de cadenas JSON; escaparlo evita cerrar el <script> que lo contiene.
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    html = TEMPLATE_PATH.read_text(encoding="utf-8")
    if html.count(DATA_MARKER) != 1:
        raise RuntimeError(f"la plantilla debe contener {DATA_MARKER} una sola vez")
    return html.replace(DATA_MARKER, data)


def write_report(path: Path, payload: dict) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_page(payload), encoding="utf-8")
    return path
