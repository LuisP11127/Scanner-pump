"""Fechas de las velas diarias.

Binance abre cada vela diaria a las 00:00 UTC y la muestra con la fecha de tu zona horaria. En Lima
(UTC-5 todo el año) la vela que abre el 2 de octubre a las 00:00 UTC empieza el 1 de octubre a las
19:00, así que Binance la muestra como la vela del **1 de octubre**, y no termina de construirse hasta
el 2 de octubre a las 19:00. Todas las fechas del scanner siguen ese criterio.

Ejemplo: el 3 de octubre a las 00:17 (hora Lima) todavía se está construyendo la vela del 2 de octubre,
así que la última vela diaria completa es la del 1 de octubre, y su fecha de análisis, el 30 de septiembre.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from typing import Optional

DAY_MS = 86_400_000

# Hora de Lima (Perú): UTC-5 todo el año, sin horario de verano.
ZONA = timezone(timedelta(hours=-5), "Lima")
ZONA_NOMBRE = "hora Lima"

MESES = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre",
         "octubre", "noviembre", "diciembre")


def now_ms() -> int:
    return int(time.time() * 1000)


def label_of(open_ms: int, zona: timezone = ZONA) -> date:
    """Fecha con la que Binance muestra la vela diaria que abre en ``open_ms`` (ms UTC)."""
    return datetime.fromtimestamp(open_ms / 1000, zona).date()


def open_of(label: date, zona: timezone = ZONA) -> int:
    """Apertura (ms UTC, siempre a las 00:00 UTC) de la vela diaria que se muestra con la fecha ``label``."""
    local_midnight = int(datetime(label.year, label.month, label.day, tzinfo=zona).timestamp() * 1000)
    return -(-local_midnight // DAY_MS) * DAY_MS  # la primera apertura de las 00:00 UTC desde la medianoche local


def last_closed_open(now: Optional[int] = None) -> int:
    """Apertura de la última vela diaria completa (la anterior a la que se está construyendo)."""
    now = now_ms() if now is None else now
    return (now // DAY_MS) * DAY_MS - DAY_MS


def last_closed_label(now: Optional[int] = None, zona: timezone = ZONA) -> date:
    return label_of(last_closed_open(now), zona)


def analysis_label(label: date) -> date:
    """Fecha de análisis: la vela anterior a la de la explosión."""
    return label - timedelta(days=1)


def long_date(label: date) -> str:
    """«1 de octubre de 2026»."""
    return f"{label.day} de {MESES[label.month - 1]} de {label.year}"


def short_date(label: date) -> str:
    """«1 oct»."""
    return f"{label.day} {MESES[label.month - 1][:3]}"


def utc_text(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def local_text(ms: int, zona: timezone = ZONA) -> str:
    return datetime.fromtimestamp(ms / 1000, zona).strftime("%Y-%m-%d %H:%M")
