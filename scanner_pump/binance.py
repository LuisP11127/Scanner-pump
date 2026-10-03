"""Cliente mínimo de la API pública de Binance (spot y futuros USDⓈ-M).

Solo usa endpoints públicos de datos de mercado, así que no necesita API key:

- spot: ``https://data-api.binance.vision`` (la dirección de Binance para datos públicos, que también
  responde desde servidores en la nube como GitHub Actions); si falla, ``https://api.binance.com``;
- futuros: ``https://www.binance.com`` (la web de Binance sirve la misma API ``/fapi/v1``); si rechaza la
  conexión, ``https://fapi.binance.com``.

Respeta el límite de peso por minuto de cada mercado leyendo la cabecera ``X-MBX-USED-WEIGHT-1M`` y
reintenta ante 429 / errores de red.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

import requests
from requests.adapters import HTTPAdapter


class BinanceError(RuntimeError):
    """Error al hablar con Binance."""


class BinanceFatalError(BinanceError):
    """Error que impide seguir escaneando el mercado (IP bloqueada, región restringida)."""


@dataclass(frozen=True)
class Market:
    key: str
    label: str
    base_url: str
    exchange_info_path: str
    klines_path: str
    default_weight_limit: int
    exchange_info_weight: int
    max_klines: int
    klines_weight: Callable[[int], int]
    # Direcciones alternativas si la principal rechaza la conexión (451/403 o una página que no es la API).
    fallback_urls: tuple[str, ...] = ()


def _futures_klines_weight(limit: int) -> int:
    if limit < 100:
        return 1
    if limit < 500:
        return 2
    if limit <= 1000:
        return 5
    return 10


SPOT = Market(
    key="spot",
    label="Spot",
    base_url="https://data-api.binance.vision",
    exchange_info_path="/api/v3/exchangeInfo",
    klines_path="/api/v3/klines",
    default_weight_limit=6000,
    exchange_info_weight=20,
    max_klines=1000,
    klines_weight=lambda limit: 2,
    fallback_urls=("https://api.binance.com",),
)

FUTURES = Market(
    key="futures",
    label="Futuros",
    base_url="https://www.binance.com",
    exchange_info_path="/fapi/v1/exchangeInfo",
    klines_path="/fapi/v1/klines",
    default_weight_limit=2400,
    exchange_info_weight=1,
    max_klines=1500,
    klines_weight=_futures_klines_weight,
    fallback_urls=("https://fapi.binance.com",),
)

MARKETS = {SPOT.key: SPOT, FUTURES.key: FUTURES}

# Lista de productos de la web de Binance: trae las etiquetas de cada par spot (p. ej. "bStocks").
PRODUCTS_URL = "https://www.binance.com/bapi/asset/v2/public/asset-service/product/get-products?includeEtf=true"
USER_AGENT = "Mozilla/5.0 (compatible; Scanner-pump)"


class WeightLimiter:
    """Reparte el peso de peticiones por minuto entre varios hilos.

    Binance cuenta el peso en ventanas de un minuto de reloj. Guardamos un margen de seguridad y
    sincronizamos el contador con lo que devuelve el servidor en cada respuesta.
    """

    def __init__(self, limit_per_minute: int, safety: float = 0.85) -> None:
        self._lock = threading.Lock()
        self._safety = safety
        self._minute = self._current_minute()
        self._used = 0
        self.set_limit(limit_per_minute)

    @staticmethod
    def _current_minute() -> int:
        return int(time.time() // 60)

    def set_limit(self, limit_per_minute: int) -> None:
        with self._lock:
            self._budget = max(1, int(limit_per_minute * self._safety))

    def acquire(self, weight: int) -> None:
        while True:
            with self._lock:
                now = time.time()
                minute = int(now // 60)
                if minute != self._minute:
                    self._minute = minute
                    self._used = 0
                if self._used + weight <= self._budget or self._used == 0:
                    self._used += weight
                    return
                wait = (minute + 1) * 60 - now + 0.25
            time.sleep(wait)

    def sync(self, used_weight: int) -> None:
        with self._lock:
            if self._current_minute() == self._minute:
                self._used = max(self._used, used_weight)


class BinanceClient:
    def __init__(
        self,
        market: Market,
        base_url: Optional[str] = None,
        timeout: float = 15.0,
        max_retries: int = 4,
        pool_size: int = 16,
    ) -> None:
        self.market = market
        self.base_url = (base_url or market.base_url).rstrip("/")
        # Con una URL elegida a mano no se prueba ninguna otra.
        self.fallback_urls = [] if base_url else list(market.fallback_urls)
        self.timeout = timeout
        self.max_retries = max_retries
        self.limiter = WeightLimiter(market.default_weight_limit)
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        adapter = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self._switch_lock = threading.Lock()

    def _switch_base(self, failed_base: str) -> bool:
        """Pasa a la siguiente dirección si la que falló sigue siendo la actual (varios hilos a la vez)."""
        with self._switch_lock:
            if self.base_url != failed_base:
                return True  # otro hilo ya cambió de dirección
            if not self.fallback_urls:
                return False
            self.base_url = self.fallback_urls.pop(0)
            return True

    def _get(
        self,
        path: str,
        params: Optional[dict] = None,
        weight: int = 1,
        valid: Optional[Callable[[Any], bool]] = None,
    ) -> Any:
        """GET a la API. ``valid`` comprueba la forma de la respuesta; si no la tiene, se prueba otra dirección."""
        last_error = "sin respuesta"
        for attempt in range(self.max_retries + 2):
            base = self.base_url
            self.limiter.acquire(weight)
            try:
                resp = self.session.get(base + path, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                last_error = str(exc)
                time.sleep(min(2 ** attempt, 16))
                continue

            used = resp.headers.get("X-MBX-USED-WEIGHT-1M")
            if used and used.isdigit():
                self.limiter.sync(int(used))

            if resp.status_code == 200:
                try:
                    data = resp.json()
                    if valid is None or valid(data):
                        return data
                except ValueError:
                    pass
                # Una página o un JSON que no es la API (p. ej. una comprobación anti-robots de la web de Binance).
                last_error = f"{base} no devolvió datos de la API"
                if self._switch_base(base):
                    continue
                raise BinanceFatalError(f"{last_error}. Prueba desde otra red o cambia la URL base.")
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After", "")
                time.sleep(int(retry_after) if retry_after.isdigit() else 60)
                last_error = "429 demasiadas peticiones"
                continue
            if resp.status_code == 418:
                raise BinanceFatalError(
                    "Binance ha bloqueado temporalmente esta IP por exceso de peticiones (418). "
                    "Espera unos minutos y reduce --workers."
                )
            if resp.status_code in (202, 403, 451):
                last_error = f"{resp.status_code} ubicación restringida en {base}"
                if self._switch_base(base):
                    continue
                raise BinanceFatalError(
                    f"Binance rechaza las peticiones desde esta ubicación ({resp.status_code}) en {base}. "
                    "Prueba desde otra red o cambia la URL base (--spot-url / --futures-url)."
                )
            if resp.status_code >= 500:
                last_error = f"{resp.status_code} {resp.text[:200]}"
                time.sleep(min(2 ** attempt, 16))
                continue
            raise BinanceError(f"{resp.status_code} en {path} {params or ''}: {resp.text[:200]}")
        raise BinanceError(f"{path} {params or ''} falló tras varios intentos: {last_error}")

    def exchange_info(self) -> dict:
        info = self._get(self.market.exchange_info_path, weight=self.market.exchange_info_weight,
                         valid=lambda d: isinstance(d, dict) and isinstance(d.get("symbols"), list))
        for rule in info.get("rateLimits", []):
            if (
                rule.get("rateLimitType") == "REQUEST_WEIGHT"
                and rule.get("interval") == "MINUTE"
                and rule.get("intervalNum") == 1
            ):
                self.limiter.set_limit(int(rule["limit"]))
        return info

    def asset_tags(self) -> Optional[dict[str, list[str]]]:
        """Etiquetas que la web de Binance muestra para cada par spot, o ``None`` si no responde.

        No es un endpoint de la API oficial, así que cualquier fallo se trata como "sin datos".
        """
        try:
            resp = self.session.get(PRODUCTS_URL, timeout=self.timeout)
            resp.raise_for_status()
            products = resp.json().get("data")
            tags = {p["s"]: list(p.get("tags") or []) for p in products if isinstance(p, dict) and "s" in p}
            return tags or None  # una respuesta vacía o con otro formato no sirve para filtrar
        except (requests.RequestException, ValueError, AttributeError, TypeError, KeyError):
            return None

    def klines(
        self,
        symbol: str,
        interval: str,
        limit: int,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
    ) -> list[list]:
        limit = max(1, min(limit, self.market.max_klines))
        params: dict[str, Any] = {"symbol": symbol, "interval": interval, "limit": limit}
        if start_time is not None:
            params["startTime"] = int(start_time)
        if end_time is not None:
            params["endTime"] = int(end_time)
        return self._get(self.market.klines_path, params=params, weight=self.market.klines_weight(limit),
                         valid=lambda d: isinstance(d, list))
