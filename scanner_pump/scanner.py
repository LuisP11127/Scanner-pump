"""Lógica del scanner: velas diarias con una subida del 10 % o más.

Una vela diaria entra en la lista si cumple alguna de estas dos medidas:

- **Mín→Máx**: la mayor subida dentro de la vela desde un mínimo hasta un máximo **posterior**. Si el
  máximo del día llegó antes que el mínimo (p. ej. máximo a las 09:00 UTC y mínimo a las 12:00 UTC) eso
  fue una caída, no una subida: cuenta solo lo que subió después del mínimo (hasta el máximo de las 15:00,
  por ejemplo) o lo que había subido antes del máximo desde un mínimo anterior.
- **Apertura→Cierre**: del precio de apertura al de cierre, ``(cierre - apertura) / apertura``.

La vela diaria de Binance no dice a qué hora fueron su mínimo y su máximo, así que el orden se saca de las
velas de 5 minutos de ese día. Solo se descargan para las monedas cuyo rango del día (máximo − mínimo,
sin mirar el orden) llega al 10 %: es el límite de lo que puede haber subido.

Toda vela con Apertura→Cierre ≥ 10 % tiene también Mín→Máx ≥ 10 % (la apertura es un mínimo anterior al
cierre), así que la lista son las velas con Mín→Máx ≥ 10 %, y de cada una se indica también si cumple
Apertura→Cierre.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from .binance import BinanceError, BinanceFatalError, Market
from .fechas import DAY_MS, analysis_label, label_of

MIN_PCT = 10.0

# Velas con las que se ordena en el tiempo el mínimo y el máximo de cada día.
INTRADAY = "5m"
INTRADAY_MS = 300_000
INTRADAY_LIMIT = DAY_MS // INTRADAY_MS  # 288 velas: el día entero en una petición

# Temporalidades de los gráficos y su duración en ms.
CHART_INTERVALS = ("2h", "4h", "8h", "12h", "1d")
INTERVAL_MS = {"2h": 7_200_000, "4h": 14_400_000, "8h": 28_800_000, "12h": 43_200_000, "1d": DAY_MS}
# Velas de cada gráfico: CHART_BEFORE antes de la explosión y el resto después (o hasta hoy).
CHART_BEFORE = 250
CHART_LIMIT = 330

# Bases que no son criptomonedas "de verdad" para este análisis (se mueven en torno a 1).
STABLECOINS = frozenset(
    {
        "USDT", "USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USDS", "USDE", "PYUSD",
        "USD1", "RLUSD", "XUSD", "BFUSD", "EUR", "EURI", "AEUR", "GBP",
    }
)

# Subyacentes de futuros que no son criptomonedas (índices, materias primas, acciones...).
NON_CRYPTO_UNDERLYING = frozenset({"INDEX", "COMMODITY", "EQUITY", "STOCK", "FOREX"})


@dataclass(frozen=True)
class ScanConfig:
    open_ms: int  # apertura (00:00 UTC) de la vela diaria a analizar
    min_pct: float = MIN_PCT
    min_quote_volume: float = 0.0  # volumen de la vela, en la moneda de cotización
    quote: str = "USDT"
    include_stables: bool = False
    include_stocks: bool = False  # acciones tokenizadas de Binance (bStocks)
    workers: int = 8


@dataclass(frozen=True)
class SymbolInfo:
    symbol: str
    base: str
    quote: str


@dataclass(frozen=True)
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    quote_volume: float

    @property
    def pct_extremes(self) -> Optional[float]:
        """Del mínimo al máximo sin mirar el orden, en %: el límite de lo que pudo subir la vela."""
        return (self.high - self.low) / self.low * 100 if self.low > 0 else None

    @property
    def pct_cuerpo(self) -> Optional[float]:
        """Variación de la apertura al cierre, en %."""
        return (self.close - self.open) / self.open * 100 if self.open > 0 else None

    @classmethod
    def from_kline(cls, k: Sequence) -> "Candle":
        quote_volume = float(k[7]) if len(k) > 7 else float(k[5]) * float(k[4])
        return cls(int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), quote_volume)


@dataclass(frozen=True)
class RunUp:
    """La mayor subida desde un mínimo hasta un máximo posterior."""

    pct: float
    low: float
    low_time: int  # apertura (ms) de la vela de 5 minutos del mínimo
    high: float
    high_time: int  # apertura (ms) de la vela de 5 minutos del máximo


def run_up(klines: Sequence[Sequence]) -> Optional[RunUp]:
    """Mayor subida de un mínimo a un máximo posterior, con velas cortas en orden.

    Dentro de cada vela se supone el recorrido habitual: si es alcista, apertura → mínimo → máximo → cierre;
    si es bajista, apertura → máximo → mínimo → cierre.
    """
    best: Optional[RunUp] = None
    low = low_time = None
    for k in sorted(klines, key=lambda k: int(k[0])):
        t, o, hi, lo, c = int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4])
        for price in ((o, lo, hi, c) if c >= o else (o, hi, lo, c)):
            if price <= 0:
                continue
            if low is None or price < low:
                low, low_time = price, t
            pct = (price - low) / low * 100
            if best is None or pct > best.pct:
                best = RunUp(pct, low, low_time, price, t)
    return best


@dataclass(frozen=True)
class Explosion:
    market: str
    symbol: str
    base: str
    quote: str
    candle: Candle  # la vela de la explosión
    analysis: Optional[Candle]  # la vela del día anterior (fecha de análisis), si existe
    rise: RunUp  # subida del mínimo a un máximo posterior dentro de la vela

    @property
    def pct_rango(self) -> float:
        return self.rise.pct

    @property
    def pct_cuerpo(self) -> float:
        return self.candle.pct_cuerpo or 0.0


@dataclass
class ScanResult:
    market: Market
    config: ScanConfig
    explosions: list[Explosion] = field(default_factory=list)
    total_symbols: int = 0
    analyzed: int = 0
    no_candle: int = 0  # pares sin vela ese día (todavía no cotizaban o estaban suspendidos)
    skipped_volume: int = 0
    skipped_stocks: int = 0
    max_before_min: int = 0  # su rango llegaba, pero el máximo fue antes del mínimo: no subió de verdad
    errors: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # símbolo -> temporalidad -> velas [apertura ms, open, high, low, close, volumen], para los gráficos
    charts: dict[str, dict[str, list[list]]] = field(default_factory=dict)


# Órdenes disponibles para mostrar o exportar las explosiones.
SORT_KEYS = {
    "rango": lambda e: -e.pct_rango,
    "cuerpo": lambda e: -e.pct_cuerpo,
    "volumen": lambda e: -e.candle.quote_volume,
    "simbolo": lambda e: e.symbol,
}


def may_explode(candle: Candle, min_pct: float = MIN_PCT) -> bool:
    """¿Pudo subir ``min_pct`` %? Su rango (máximo − mínimo, sin orden) es el límite de la subida."""
    extremes = candle.pct_extremes
    return extremes is not None and extremes >= min_pct


def is_explosion(candle: Candle, rise: Optional[RunUp], min_pct: float = MIN_PCT) -> bool:
    """¿Subió ``min_pct`` % de un mínimo a un máximo posterior, o de la apertura al cierre?"""
    cuerpo = candle.pct_cuerpo
    return (rise is not None and rise.pct >= min_pct) or (cuerpo is not None and cuerpo >= min_pct)


def select_symbols(
    market_key: str,
    exchange_info: dict,
    quote: Optional[str] = "USDT",
    include_stables: bool = False,
) -> list[SymbolInfo]:
    """Filtra los pares operables del exchangeInfo de spot o futuros."""
    selected = []
    for s in exchange_info.get("symbols", []):
        if s.get("status") != "TRADING":
            continue
        if quote and s.get("quoteAsset") != quote:
            continue
        if not include_stables and s.get("baseAsset") in STABLECOINS:
            continue
        if market_key == "spot":
            if not s.get("isSpotTradingAllowed", True):
                continue
        else:
            if s.get("contractType") != "PERPETUAL":
                continue
            if s.get("underlyingType") in NON_CRYPTO_UNDERLYING:
                continue
        selected.append(SymbolInfo(s["symbol"], s["baseAsset"], s["quoteAsset"]))
    return selected


def is_tokenized_stock(tags: Sequence[str]) -> bool:
    """Binance etiqueta sus acciones tokenizadas (AAPLB, NVDAB...) como "bStocks"."""
    return any(isinstance(tag, str) and "stock" in tag.lower() for tag in tags)


def parse_rows(klines: Sequence[Sequence]) -> list[list]:
    """[apertura ms, open, high, low, close, volumen] de cada vela de Binance (lo que usan los gráficos)."""
    return [[int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])] for k in klines]


def pick_day(klines: Sequence[Sequence], open_ms: int) -> tuple[Optional[Candle], Optional[Candle]]:
    """La vela que abre en ``open_ms`` y la del día anterior, de una respuesta de velas diarias."""
    by_open = {int(k[0]): k for k in klines}
    day = by_open.get(open_ms)
    prev = by_open.get(open_ms - DAY_MS)
    return (Candle.from_kline(day) if day else None, Candle.from_kline(prev) if prev else None)


def _pool_map(workers: int, func, items, progress=None):
    """Ejecuta ``func(item)`` en paralelo y devuelve [(item, resultado o excepción)] en orden de llegada."""
    out = []
    pool = ThreadPoolExecutor(max_workers=max(1, workers))
    try:
        jobs = {pool.submit(func, item): item for item in items}
        for done, job in enumerate(as_completed(jobs), 1):
            if progress:
                progress(done, len(jobs))
            try:
                out.append((jobs[job], job.result()))
            except BinanceFatalError:
                raise
            except BinanceError as exc:
                out.append((jobs[job], exc))
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    return out


def scan_market(
    client,
    config: ScanConfig,
    progress: Optional[Callable[[int, int], None]] = None,
) -> ScanResult:
    """Descarga la vela diaria de todos los pares del mercado y devuelve las que subieron ``min_pct`` % o más."""
    result = ScanResult(market=client.market, config=config)
    symbols = select_symbols(client.market.key, client.exchange_info(), config.quote, config.include_stables)
    if client.market.key == "spot" and not config.include_stocks:
        tags = client.asset_tags()
        if tags is None:
            result.warnings.append(
                "no se pudo consultar qué pares son acciones tokenizadas (bStocks); pueden aparecer en la lista"
            )
        else:
            crypto = [info for info in symbols if not is_tokenized_stock(tags.get(info.symbol, ()))]
            result.skipped_stocks = len(symbols) - len(crypto)
            symbols = crypto
    result.total_symbols = len(symbols)

    # 1) Dos velas diarias por par: la del día de análisis y la de la explosión.
    daily = _pool_map(
        config.workers,
        lambda info: client.klines(info.symbol, "1d", 2, start_time=config.open_ms - DAY_MS),
        symbols,
        progress,
    )
    candidates = []
    for info, klines in daily:
        if isinstance(klines, BinanceError):
            result.errors.append((info.symbol, str(klines)))
            continue
        candle, prev = pick_day(klines, config.open_ms)
        if candle is None:
            result.no_candle += 1
            continue
        result.analyzed += 1
        if not may_explode(candle, config.min_pct):
            continue
        if config.min_quote_volume > 0 and candle.quote_volume < config.min_quote_volume:
            result.skipped_volume += 1
            continue
        candidates.append((info, candle, prev))

    # 2) Velas de 5 minutos de ese día para saber si el mínimo fue antes que el máximo.
    intraday = _pool_map(
        config.workers,
        lambda cand: client.klines(cand[0].symbol, INTRADAY, INTRADAY_LIMIT, start_time=config.open_ms,
                                   end_time=config.open_ms + DAY_MS - 1),
        candidates,
        progress,
    )
    for (info, candle, prev), klines in intraday:
        if isinstance(klines, BinanceError):
            result.errors.append((info.symbol, str(klines)))
            continue
        rise = run_up(k for k in klines if config.open_ms <= int(k[0]) < config.open_ms + DAY_MS)
        if rise is None:
            result.errors.append((info.symbol, f"sin velas de {INTRADAY} ese día"))
            continue
        if not is_explosion(candle, rise, config.min_pct):
            result.max_before_min += 1
            continue
        result.explosions.append(Explosion(client.market.key, info.symbol, info.base, info.quote, candle, prev, rise))
    return result


def chart_window(open_ms: int, interval: str) -> tuple[int, int]:
    """(startTime, limit) para el gráfico de una explosión: velas de antes y de después de ese día."""
    return open_ms - CHART_BEFORE * INTERVAL_MS[interval], CHART_LIMIT


def fetch_charts(
    client,
    result: ScanResult,
    intervals: Sequence[str] = CHART_INTERVALS,
    progress: Optional[Callable[[int, int], None]] = None,
) -> None:
    """Descarga las velas de cada temporalidad para el gráfico de cada explosión."""
    wanted = [(e.symbol, iv) for e in result.explosions for iv in dict.fromkeys(intervals) if iv in INTERVAL_MS]
    if not wanted:
        return

    def fetch(job):
        symbol, interval = job
        start, limit = chart_window(result.config.open_ms, interval)
        return client.klines(symbol, interval, limit, start_time=start)

    try:
        rows = _pool_map(result.config.workers, fetch, wanted, progress)
    except BinanceFatalError as exc:
        result.warnings.append(f"no se pudieron descargar los gráficos: {exc}")
        return
    for (symbol, interval), klines in rows:
        if isinstance(klines, BinanceError) or not klines:
            continue  # el gráfico de esa temporalidad simplemente no estará disponible
        result.charts.setdefault(symbol, {})[interval] = parse_rows(klines)


def explosion_to_dict(e: Explosion) -> dict:
    """Una explosión como la guardan el seguimiento, el informe y la web."""
    fecha = label_of(e.candle.open_time)
    c = e.candle
    data = {
        "mercado": e.market,
        "simbolo": e.symbol,
        "base": e.base,
        "quote": e.quote,
        "fecha": fecha.isoformat(),
        "fecha_analisis": analysis_label(fecha).isoformat(),
        "apertura": c.open_time,
        "open": c.open,
        "high": c.high,
        "low": c.low,
        "close": c.close,
        "volumen": round(c.quote_volume, 2),
        "pct_rango": round(e.pct_rango, 4),
        "pct_cuerpo": round(e.pct_cuerpo, 4),
        "min_precio": e.rise.low,
        "min_hora": e.rise.low_time,
        "max_precio": e.rise.high,
        "max_hora": e.rise.high_time,
        "intradia": INTRADAY,
        "analisis": None,
    }
    if e.analysis:
        a = e.analysis
        data["analisis"] = {
            "open": a.open, "high": a.high, "low": a.low, "close": a.close,
            "volumen": round(a.quote_volume, 2),
            "pct_cuerpo": round(a.pct_cuerpo or 0.0, 4),
        }
    return data
