import json
from datetime import date, datetime, timezone

from scanner_pump import listados, web
from scanner_pump.binance import FUTURES, SPOT, BinanceError, BinanceFatalError
from scanner_pump.listados import (count_listed, desde_ms, listing_symbols, read_listings, update_listings,
                                   write_listings)


def utc_ms(*args) -> int:
    return int(datetime(*args, tzinfo=timezone.utc).timestamp() * 1000)


NOW = utc_ms(2026, 10, 15, 12)
OLD = utc_ms(2021, 5, 3, 8)
MARCH = utc_ms(2026, 3, 12, 8)      # listada el 12 mar 2026 a las 08:00 UTC
OCTOBER = utc_ms(2026, 10, 3, 9)


def spot_info():
    s = lambda sym, base, status="TRADING", **kw: dict(symbol=sym, baseAsset=base, quoteAsset="USDT", status=status, **kw)
    return {"symbols": [
        s("BTCUSDT", "BTC"),
        s("NEWUSDT", "NEW"),
        s("GONEUSDT", "GONE", status="BREAK"),         # listada en 2026 y retirada: también cuenta
        s("SOONUSDT", "SOON", status="PRE_TRADING"),   # todavía no cotiza
        s("USDCUSDT", "USDC"),                         # stablecoin
        s("AAPLBUSDT", "AAPLB"),                       # acción tokenizada
        dict(symbol="NEWBTC", baseAsset="NEW", quoteAsset="BTC", status="TRADING"),
    ]}


def futures_info():
    f = lambda sym, base, onboard, **kw: dict(symbol=sym, baseAsset=base, quoteAsset="USDT", status="TRADING",
                                              contractType="PERPETUAL", onboardDate=onboard, **kw)
    return {"symbols": [
        f("BTCUSDT", "BTC", utc_ms(2019, 9, 25)),
        f("ZEDUSDT", "ZED", OCTOBER),
        f("XAUUSDT", "XAU", OCTOBER, underlyingType="COMMODITY"),
        dict(f("ZEDUSDT_261225", "ZED", OCTOBER), contractType="CURRENT_QUARTER"),
    ]}


class FakeClient:
    def __init__(self, market, info, first, tags=None, fail=()):
        self.market = market
        self._info = info
        self._first = first  # símbolo -> (apertura de la primera vela, precio de apertura)
        self._tags = tags
        self._fail = set(fail)
        self.calls = []

    def exchange_info(self):
        if isinstance(self._info, Exception):
            raise self._info
        return self._info

    def asset_tags(self):
        return self._tags

    def klines(self, symbol, interval, limit, start_time=None, end_time=None):
        self.calls.append((symbol, interval, limit, start_time))
        if symbol in self._fail:
            raise BinanceError("timeout")
        t, price = self._first[symbol]
        if start_time and start_time > t:
            t = start_time
        return [[t, str(price), "0", "0", "0", "0", t + 59_999, "0", 0, "0", "0", "0"]]


def clients(**kw):
    return {
        "spot": FakeClient(SPOT, spot_info(), {"BTCUSDT": (OLD, 50000.0), "NEWUSDT": (MARCH, 0.12), "GONEUSDT": (MARCH + 3_600_000, 1.5)},
                           tags={"AAPLBUSDT": ["bStocks"]}, **kw.get("spot", {})),
        "futures": FakeClient(FUTURES, kw.get("futures_info", futures_info()), {"ZEDUSDT": (OCTOBER, 0.45)}),
    }


def test_desde_is_midnight_in_lima():
    assert desde_ms() == utc_ms(2026, 1, 1, 5)


def test_listing_symbols_filters():
    assert [s["symbol"] for s in listing_symbols("spot", spot_info())] == ["BTCUSDT", "NEWUSDT", "GONEUSDT", "AAPLBUSDT"]
    futures = listing_symbols("futures", futures_info())
    assert [(s["symbol"], s["onboard"]) for s in futures] == [("BTCUSDT", utc_ms(2019, 9, 25)), ("ZEDUSDT", OCTOBER)]


def test_update_listings_from_scratch():
    cl = clients()
    pares, warnings, asked = update_listings(cl, {}, now=NOW, workers=2)
    assert warnings == []
    assert pares["spot:BTCUSDT"] == {"base": "BTC", "listado": OLD}  # anterior a 2026: sin precio inicial
    assert pares["spot:NEWUSDT"] == {"base": "NEW", "listado": MARCH, "precio_inicial": 0.12}
    assert pares["spot:GONEUSDT"]["listado"] == MARCH + 3_600_000
    assert pares["futures:ZEDUSDT"] == {"base": "ZED", "listado": OCTOBER, "precio_inicial": 0.45}
    assert "spot:AAPLBUSDT" not in pares and "futures:XAUUSDT" not in pares
    # Spot: la primera vela de 1 minuto desde el principio; futuros: la del día del lanzamiento.
    assert ("NEWUSDT", "1m", 1, 0) in cl["spot"].calls
    assert cl["futures"].calls == [("ZEDUSDT", "1m", 1, OCTOBER)]
    assert asked == 4
    assert count_listed(pares, now=NOW) == {"spot": 2, "futures": 1}


def test_update_listings_only_asks_whats_missing():
    first, _, _ = update_listings(clients(), {}, now=NOW)
    cl = clients()
    pares, _, asked = update_listings(cl, first, now=NOW)
    assert pares == first
    assert asked == 0 and cl["spot"].calls == [] and cl["futures"].calls == []


def test_update_listings_keeps_known_pairs_when_a_market_fails():
    first, _, _ = update_listings(clients(), {}, now=NOW)
    pares, warnings, _ = update_listings(clients(futures_info=BinanceFatalError("451")), first, now=NOW)
    assert pares["futures:ZEDUSDT"]["precio_inicial"] == 0.45  # se conserva lo que ya se sabía
    assert any("451" in w for w in warnings)


def test_update_listings_retries_failed_pairs_later():
    pares, warnings, _ = update_listings(clients(spot={"fail": {"NEWUSDT"}}), {}, now=NOW)
    assert "spot:NEWUSDT" not in pares and any("no respondieron" in w for w in warnings)
    pares, _, _ = update_listings(clients(), pares, now=NOW)
    assert pares["spot:NEWUSDT"]["precio_inicial"] == 0.12


def test_write_and_read_listings(tmp_path):
    pares, _, _ = update_listings(clients(), {}, now=NOW)
    path = write_listings(tmp_path / "datos" / "listados.json", pares)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["desde"] == "2026-01-01" and data["pares"] == pares
    assert read_listings(path) == pares
    assert read_listings(tmp_path / "no-existe.json") == {}
    # Sin hora de generación: si no hay pares nuevos, el archivo no cambia (y no se guarda otra vez).
    first = path.read_text(encoding="utf-8")
    assert write_listings(path, pares).read_text(encoding="utf-8") == first
    # Un par por línea, para que el historial del repositorio se lea bien.
    assert sum(1 for line in path.read_text(encoding="utf-8").splitlines() if '"listado"' in line) == len(pares)


def test_cli_updates_file(tmp_path, monkeypatch, capsys):
    cl = clients()
    monkeypatch.setattr(listados, "BinanceClient", lambda market, **kw: cl[market.key])
    monkeypatch.setattr(listados, "now_ms", lambda: NOW)
    assert listados.main([str(tmp_path / "listados.json"), "--salida", str(tmp_path / "nuevo.json")]) == 0
    out = capsys.readouterr().out
    assert "spot 2, futuros 1" in out
    assert read_listings(tmp_path / "nuevo.json")["spot:NEWUSDT"]["listado"] == MARCH


def test_web_publishes_listings(tmp_path):
    pares, _, _ = update_listings(clients(), {}, now=NOW)
    write_listings(tmp_path / "listados.json", pares)
    assert web.main([str(tmp_path / "site"), "--listados", str(tmp_path / "listados.json")]) == 0
    data = json.loads((tmp_path / "site" / "listados.json").read_text(encoding="utf-8"))
    assert data["pares"] == pares
    # Sin archivo, la web se publica igual (buscará las fechas en Binance).
    assert web.main([str(tmp_path / "site2"), "--listados", str(tmp_path / "nada.json")]) == 0
    assert not (tmp_path / "site2" / "listados.json").exists()


def test_desde_custom_year():
    assert desde_ms(date(2025, 1, 1)) == utc_ms(2025, 1, 1, 5)
