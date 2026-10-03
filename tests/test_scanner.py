import json
import re
import shutil
import subprocess
from datetime import date, datetime, timezone

import pytest

from scanner_pump import cli, web
from scanner_pump.binance import FUTURES, SPOT, BinanceFatalError
from scanner_pump.fechas import (DAY_MS, analysis_label, label_of, last_closed_label, last_closed_open, open_of)
from scanner_pump.report import DATA_MARKER, TEMPLATE_PATH, build_payload, render_page
from scanner_pump.scanner import (CHART_BEFORE, CHART_LIMIT, Candle, ScanConfig, explosion_to_dict, fetch_charts,
                                  is_explosion, pick_day, scan_market, select_symbols)
from scanner_pump.seguimiento import collect_days, merge_folder, read_day, write_day


def utc_ms(*args) -> int:
    return int(datetime(*args, tzinfo=timezone.utc).timestamp() * 1000)


def kline(open_ms, o, hi, lo, c, quote_volume=1_000_000.0):
    """Fila de velas de Binance (12 campos, como devuelve la API)."""
    return [open_ms, str(o), str(hi), str(lo), str(c), "1000", open_ms + DAY_MS - 1, str(quote_volume), 100, "0", "0", "0"]


# ---------- fechas ----------

def test_lima_label_of_daily_candle():
    # La vela que abre el 2 oct a las 00:00 UTC empieza el 1 oct a las 19:00 en Lima: es la «vela del 1 oct».
    assert label_of(utc_ms(2026, 10, 2)) == date(2026, 10, 1)
    assert open_of(date(2026, 10, 1)) == utc_ms(2026, 10, 2)


def test_open_of_roundtrip_over_a_year():
    for n in range(400):
        open_ms = utc_ms(2026, 1, 1) + n * DAY_MS
        assert open_of(label_of(open_ms)) == open_ms


def test_user_example_last_closed_candle():
    # 3 oct 00:17 hora Lima = 3 oct 05:17 UTC: se está construyendo la vela del 2 oct, la última completa es la del 1 oct.
    now = utc_ms(2026, 10, 3, 5, 17)
    assert label_of((now // DAY_MS) * DAY_MS) == date(2026, 10, 2)  # la vela en curso
    assert last_closed_label(now) == date(2026, 10, 1)
    assert analysis_label(last_closed_label(now)) == date(2026, 9, 30)


def test_last_closed_changes_at_19_lima():
    before = utc_ms(2026, 10, 3, 23, 59)  # 3 oct 18:59 Lima
    after = utc_ms(2026, 10, 4, 0, 1)     # 3 oct 19:01 Lima
    assert last_closed_label(before) == date(2026, 10, 1)
    assert last_closed_label(after) == date(2026, 10, 2)
    assert last_closed_open(after) == utc_ms(2026, 10, 3)


def test_analysis_date_is_previous_candle():
    assert analysis_label(date(2026, 10, 5)) == date(2026, 10, 4)
    assert analysis_label(date(2026, 3, 1)) == date(2026, 2, 28)


# ---------- medidas ----------

def test_candle_percentages():
    c = Candle(0, open=1.0, high=1.3, low=0.9, close=1.2, quote_volume=10)
    assert c.pct_rango == pytest.approx((1.3 - 0.9) / 0.9 * 100)
    assert c.pct_cuerpo == pytest.approx(20.0)


def test_is_explosion_by_range_or_body():
    assert is_explosion(Candle(0, 1.0, 1.10, 1.0, 1.02, 0))        # mín→máx exactamente 10 %
    assert not is_explosion(Candle(0, 1.0, 1.0999, 1.0, 1.05, 0))  # 9.99 %
    assert is_explosion(Candle(0, 1.0, 1.2, 0.95, 0.96, 0))        # sube y se desinfla: cuenta el rango
    assert not is_explosion(Candle(0, 1.0, 1.05, 0.98, 0.99, 0))
    assert is_explosion(Candle(0, 1.0, 1.3, 1.0, 1.3, 0), min_pct=30)
    assert not is_explosion(Candle(0, 1.0, 1.3, 1.0, 1.3, 0), min_pct=31)


def test_body_never_exceeds_range():
    for o, hi, lo, c in [(1, 1.5, 0.8, 1.4), (2, 2.2, 2, 2.2), (5, 6, 4, 4.5)]:
        candle = Candle(0, o, hi, lo, c, 0)
        assert candle.pct_cuerpo <= candle.pct_rango


def test_pick_day_ignores_other_dates():
    day = utc_ms(2026, 10, 2)
    rows = [kline(day - DAY_MS, 1, 1.1, 0.9, 1.05), kline(day, 1.05, 1.3, 1.0, 1.25)]
    candle, prev = pick_day(rows, day)
    assert candle.open == 1.05 and prev.close == 1.05
    # Par que empezó a cotizar más tarde: Binance devuelve velas posteriores, que no cuentan.
    assert pick_day([kline(day + DAY_MS, 1, 2, 1, 2)], day) == (None, None)


# ---------- escáner ----------

EXCHANGE_SPOT = {
    "symbols": [
        {"symbol": "AAAUSDT", "status": "TRADING", "baseAsset": "AAA", "quoteAsset": "USDT"},
        {"symbol": "BBBUSDT", "status": "TRADING", "baseAsset": "BBB", "quoteAsset": "USDT"},
        {"symbol": "CCCUSDT", "status": "TRADING", "baseAsset": "CCC", "quoteAsset": "USDT"},
        {"symbol": "NEWUSDT", "status": "TRADING", "baseAsset": "NEW", "quoteAsset": "USDT"},
        {"symbol": "LOWUSDT", "status": "TRADING", "baseAsset": "LOW", "quoteAsset": "USDT"},
        {"symbol": "AAPLBUSDT", "status": "TRADING", "baseAsset": "AAPLB", "quoteAsset": "USDT"},
        {"symbol": "USDCUSDT", "status": "TRADING", "baseAsset": "USDC", "quoteAsset": "USDT"},
        {"symbol": "AAABTC", "status": "TRADING", "baseAsset": "AAA", "quoteAsset": "BTC"},
        {"symbol": "OLDUSDT", "status": "BREAK", "baseAsset": "OLD", "quoteAsset": "USDT"},
    ]
}


class FakeClient:
    def __init__(self, market, exchange_info, daily, charts=None, tags=None):
        self.market = market
        self.base_url = market.base_url
        self._info = exchange_info
        self._daily = daily  # símbolo -> filas de velas diarias
        self._charts = charts or {}
        self._tags = tags
        self.calls = []

    def exchange_info(self):
        return self._info

    def asset_tags(self):
        return self._tags

    def klines(self, symbol, interval, limit, start_time=None, end_time=None):
        self.calls.append((symbol, interval, limit, start_time))
        if interval == "1d" and limit == 2:
            return [r for r in self._daily.get(symbol, []) if r[0] >= start_time][:limit]
        return self._charts.get((symbol, interval), [])


DAY = utc_ms(2026, 10, 2)  # vela del 1 oct (hora Lima)


def daily_rows():
    prev = DAY - DAY_MS
    return {
        "AAAUSDT": [kline(prev, 1, 1.02, 0.99, 1.0), kline(DAY, 1.0, 1.35, 0.98, 1.30, 5e6)],    # +37.8 % / +30 %
        "BBBUSDT": [kline(prev, 2, 2.1, 1.9, 2.0), kline(DAY, 2.0, 2.3, 1.9, 1.95, 2e6)],        # rango 21 %, cuerpo −2.5 %
        "CCCUSDT": [kline(prev, 3, 3.1, 2.9, 3.0), kline(DAY, 3.0, 3.1, 2.95, 3.05)],            # 5 %: no
        "NEWUSDT": [kline(DAY + DAY_MS, 1, 3, 1, 2)],                                            # sin vela ese día
        "LOWUSDT": [kline(prev, 1, 1, 1, 1), kline(DAY, 1.0, 1.5, 1.0, 1.4, 1000)],              # poco volumen
        "AAPLBUSDT": [kline(prev, 1, 1, 1, 1), kline(DAY, 1.0, 1.5, 1.0, 1.4, 5e6)],             # acción tokenizada
    }


def test_select_symbols_filters():
    spot = [s.symbol for s in select_symbols("spot", EXCHANGE_SPOT)]
    assert spot == ["AAAUSDT", "BBBUSDT", "CCCUSDT", "NEWUSDT", "LOWUSDT", "AAPLBUSDT"]
    futures = {
        "symbols": [
            {"symbol": "XUSDT", "status": "TRADING", "baseAsset": "X", "quoteAsset": "USDT", "contractType": "PERPETUAL"},
            {"symbol": "X_260327", "status": "TRADING", "baseAsset": "X", "quoteAsset": "USDT", "contractType": "CURRENT_QUARTER"},
            {"symbol": "XAUUSDT", "status": "TRADING", "baseAsset": "XAU", "quoteAsset": "USDT", "contractType": "PERPETUAL",
             "underlyingType": "COMMODITY"},
        ]
    }
    assert [s.symbol for s in select_symbols("futures", futures)] == ["XUSDT"]


def test_scan_market_finds_explosions():
    client = FakeClient(SPOT, EXCHANGE_SPOT, daily_rows(), tags={"AAPLBUSDT": ["bStocks"]})
    result = scan_market(client, ScanConfig(open_ms=DAY, min_quote_volume=10_000, workers=2))
    found = {e.symbol: e for e in result.explosions}
    assert set(found) == {"AAAUSDT", "BBBUSDT"}
    assert found["AAAUSDT"].pct_cuerpo == pytest.approx(30.0)
    assert found["BBBUSDT"].pct_rango == pytest.approx((2.3 - 1.9) / 1.9 * 100)
    assert found["BBBUSDT"].analysis.close == 2.0
    assert result.analyzed == 4  # AAA, BBB, CCC y LOW tenían vela ese día
    assert result.no_candle == 1
    assert result.skipped_volume == 1
    assert result.skipped_stocks == 1
    # Cada par pide dos velas diarias desde el día de análisis.
    assert ("AAAUSDT", "1d", 2, DAY - DAY_MS) in client.calls


def test_scan_market_warns_without_stock_list():
    client = FakeClient(SPOT, EXCHANGE_SPOT, daily_rows(), tags=None)
    result = scan_market(client, ScanConfig(open_ms=DAY))
    assert "AAPLBUSDT" in {e.symbol for e in result.explosions}
    assert result.warnings


def test_scan_market_propagates_fatal_errors():
    class Blocked(FakeClient):
        def klines(self, *args, **kwargs):
            raise BinanceFatalError("451")

    with pytest.raises(BinanceFatalError):
        scan_market(Blocked(FUTURES, {"symbols": []} | {"symbols": [
            {"symbol": "XUSDT", "status": "TRADING", "baseAsset": "X", "quoteAsset": "USDT", "contractType": "PERPETUAL"}]}, {}),
            ScanConfig(open_ms=DAY))


def test_fetch_charts_window_around_explosion():
    rows = [kline(DAY - 5 * 7_200_000, 1, 1, 1, 1)]
    client = FakeClient(SPOT, EXCHANGE_SPOT, daily_rows(), charts={("AAAUSDT", "2h"): rows}, tags={})
    result = scan_market(client, ScanConfig(open_ms=DAY))
    fetch_charts(client, result, ["2h"])
    assert result.charts["AAAUSDT"]["2h"] == [[rows[0][0], 1.0, 1.0, 1.0, 1.0, 1000.0]]
    assert ("AAAUSDT", "2h", CHART_LIMIT, DAY - CHART_BEFORE * 7_200_000) in client.calls


def test_explosion_to_dict():
    client = FakeClient(SPOT, EXCHANGE_SPOT, daily_rows(), tags={})
    result = scan_market(client, ScanConfig(open_ms=DAY))
    data = explosion_to_dict(next(e for e in result.explosions if e.symbol == "AAAUSDT"))
    assert data["fecha"] == "2026-10-01"
    assert data["fecha_analisis"] == "2026-09-30"
    assert data["apertura"] == DAY
    assert data["pct_cuerpo"] == pytest.approx(30.0)
    assert data["analisis"]["close"] == 1.0
    assert data["volumen"] == 5e6


# ---------- seguimiento ----------

def coin(symbol, rango, market="spot", **extra):
    return {"mercado": market, "simbolo": symbol, "base": symbol[:-4], "quote": "USDT", "fecha": "2026-10-01",
            "fecha_analisis": "2026-09-30", "apertura": DAY, "pct_rango": rango, "pct_cuerpo": 1.0, **extra}


def test_write_day_merges_with_existing(tmp_path):
    write_day(tmp_path, date(2026, 10, 1), [coin("AAAUSDT", 12.0)])
    path = write_day(tmp_path, date(2026, 10, 1), [coin("BBBUSDT", 30.0), coin("AAAUSDT", 13.0), coin("AAAUSDT", 15.0, "futures")])
    day = read_day(path)
    assert path.name == "2026-10-01.json"
    assert day["fecha_analisis"] == "2026-09-30"
    assert day["apertura_utc"] == "2026-10-02T00:00:00Z"
    assert [(c["mercado"], c["simbolo"], c["pct_rango"]) for c in day["monedas"]] == [
        ("spot", "BBBUSDT", 30.0), ("futures", "AAAUSDT", 15.0), ("spot", "AAAUSDT", 13.0)]


def test_collect_days_newest_first(tmp_path):
    write_day(tmp_path, date(2026, 9, 30), [coin("AAAUSDT", 12.0)])
    write_day(tmp_path, date(2026, 10, 1), [coin("BBBUSDT", 20.0)])
    (tmp_path / "roto.json").write_text("{", encoding="utf-8")
    (tmp_path / "README.md").write_text("# no es un día", encoding="utf-8")
    days = collect_days(tmp_path)
    assert [d["fecha"] for d in days] == ["2026-10-01", "2026-09-30"]
    assert days[1]["fecha_analisis"] == "2026-09-29"


def test_merge_folder(tmp_path):
    src, dst = tmp_path / "nuevo", tmp_path / "repo"
    write_day(dst, date(2026, 10, 1), [coin("AAAUSDT", 12.0, "futures")])  # lo guardó la web
    write_day(src, date(2026, 10, 1), [coin("BBBUSDT", 20.0)])              # lo encontró el workflow
    merge_folder(src, dst)
    day = read_day(dst / "2026-10-01.json")
    assert {(c["mercado"], c["simbolo"]) for c in day["monedas"]} == {("futures", "AAAUSDT"), ("spot", "BBBUSDT")}


# ---------- informe y web ----------

def test_render_page_embeds_payload_safely():
    html = render_page({"mercados": {}, "nota": "</script><b>"})
    assert DATA_MARKER not in html
    assert "</script><b>" not in html
    assert '"\\u003c/script>\\u003cb>"' in html
    assert TEMPLATE_PATH.read_text(encoding="utf-8").count(DATA_MARKER) == 1


def test_build_payload():
    client = FakeClient(SPOT, EXCHANGE_SPOT, daily_rows(), tags={"AAPLBUSDT": ["bStocks"]})
    result = scan_market(client, ScanConfig(open_ms=DAY))
    result.charts["AAAUSDT"] = {"1d": [[DAY, 1, 2, 1, 2, 5]]}
    payload = build_payload([result], datetime(2026, 10, 3, tzinfo=timezone.utc))
    assert payload["fecha"] == "2026-10-01" and payload["fecha_analisis"] == "2026-09-30"
    coins = payload["mercados"]["spot"]["coincidencias"]
    assert [c["pct_rango"] for c in coins] == sorted((c["pct_rango"] for c in coins), reverse=True)
    by_symbol = {c["simbolo"]: c for c in coins}
    assert by_symbol["AAAUSDT"]["graficos"]["1d"] and "graficos" not in by_symbol["BBBUSDT"]
    assert payload["mercados"]["spot"]["analizadas"] == result.analyzed


def test_web_build(tmp_path):
    write_day(tmp_path / "seg", date(2026, 10, 1), [coin("AAAUSDT", 12.0)])
    assert web.main([str(tmp_path / "site"), "--seguimiento", str(tmp_path / "seg")]) == 0
    html = (tmp_path / "site" / "index.html").read_text(encoding="utf-8")
    assert '<script id="scanner-data" type="application/json">null</script>' in html
    data = json.loads((tmp_path / "site" / "seguimiento.json").read_text(encoding="utf-8"))
    assert data["dias"][0]["fecha"] == "2026-10-01"


# ---------- línea de comandos ----------

def test_cli_scan_writes_tracking_and_report(tmp_path, monkeypatch, capsys):
    clients = {
        "spot": FakeClient(SPOT, EXCHANGE_SPOT, daily_rows(), tags={}),
        "futures": FakeClient(FUTURES, {"symbols": []}, {}),
    }
    monkeypatch.setattr(cli, "BinanceClient", lambda market, **kw: clients[market.key])
    code = cli.main(["--fecha", "2026-10-01", "--seguimiento", str(tmp_path / "seg"), "--json", str(tmp_path / "r.json"),
                     "--html", str(tmp_path / "r.html"), "--no-abrir"])
    out = capsys.readouterr().out
    assert code == 0
    assert "vela del 1 de octubre de 2026" in out
    assert "AAAUSDT" in out and "CCCUSDT" not in out
    day = read_day(tmp_path / "seg" / "2026-10-01.json")
    assert {c["simbolo"] for c in day["monedas"]} >= {"AAAUSDT", "BBBUSDT"}
    assert json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))["fecha"] == "2026-10-01"
    assert "AAAUSDT" in (tmp_path / "r.html").read_text(encoding="utf-8")


def test_cli_rejects_unfinished_candle(capsys):
    future = (last_closed_label().toordinal() + 1)
    with pytest.raises(SystemExit):
        cli.main(["--fecha", date.fromordinal(future).isoformat(), "--sin-grafico"])
    assert "todavía no ha terminado" in capsys.readouterr().err


def test_cli_partial_failure_returns_error(monkeypatch, tmp_path):
    class Blocked(FakeClient):
        def exchange_info(self):
            raise BinanceFatalError("451")

    clients = {"spot": FakeClient(SPOT, EXCHANGE_SPOT, daily_rows(), tags={}), "futures": Blocked(FUTURES, {}, {})}
    monkeypatch.setattr(cli, "BinanceClient", lambda market, **kw: clients[market.key])
    code = cli.main(["--fecha", "2026-10-01", "--seguimiento", str(tmp_path), "--sin-grafico"])
    assert code == 1  # futuros falló...
    assert (tmp_path / "2026-10-01.json").exists()  # ...pero spot se guardó


# ---------- la página (JavaScript) ----------

def page_script() -> str:
    html = TEMPLATE_PATH.read_text(encoding="utf-8")
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert len(scripts) == 1
    return scripts[0]


@pytest.mark.skipif(not shutil.which("node"), reason="hace falta Node.js")
def test_page_script_syntax(tmp_path):
    path = tmp_path / "page.js"
    path.write_text(page_script(), encoding="utf-8")
    subprocess.run(["node", "--check", str(path)], check=True)


@pytest.mark.skipif(not shutil.which("node"), reason="hace falta Node.js")
def test_page_dates_match_python():
    script = page_script()
    pieces = [re.search(r"\n  const DAY = .*?;\n", script).group(0),
              re.search(r"\n  const ZONA = .*?;\n", script).group(0),
              re.search(r"\n  function labelOf\(ms\).*?\n", script).group(0),
              re.search(r"\n  function openOf\(label\).*?\n  }\n", script, re.S).group(0),
              re.search(r"\n  function lastClosedOpen\(now\).*?\n", script).group(0)]
    opens = [utc_ms(2026, 1, 1) + n * DAY_MS for n in range(0, 400, 7)]
    nows = [utc_ms(2026, 10, 3, 5, 17), utc_ms(2026, 10, 3, 23, 59), utc_ms(2026, 10, 4, 0, 1)]
    js = "".join(pieces) + (
        f"console.log(JSON.stringify({{labels: {opens}.map(labelOf), opens: {opens}.map((o) => openOf(labelOf(o))),"
        f" last: {nows}.map((n) => labelOf(lastClosedOpen(n)))}}));"
    )
    result = json.loads(subprocess.run(["node", "-e", js], check=True, capture_output=True, text=True).stdout)
    assert result["labels"] == [label_of(o).isoformat() for o in opens]
    assert result["opens"] == opens
    assert result["last"] == [last_closed_label(n).isoformat() for n in nows]


# ---------- cliente de Binance ----------

class FakeResponse:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.headers = {}
        self.text = json.dumps(body) if not isinstance(body, str) else body

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("no es JSON")
        return self._body


def client_with(responses):
    """BinanceClient de futuros cuya sesión responde según la dirección: {base: FakeResponse}."""
    from scanner_pump.binance import BinanceClient

    client = BinanceClient(FUTURES)
    seen = []

    def get(url, params=None, timeout=None):
        seen.append(url)
        return next(r for base, r in responses.items() if url.startswith(base))

    client.session.get = get
    return client, seen


@pytest.mark.parametrize("blocked", [
    FakeResponse(200, {}),                        # JSON que no es la API
    FakeResponse(200, "<html>captcha</html>"),    # una página
    FakeResponse(451, {"msg": "restricted"}),     # ubicación restringida
])
def test_client_falls_back_to_fapi(blocked):
    client, seen = client_with({
        "https://www.binance.com": blocked,
        "https://fapi.binance.com": FakeResponse(200, {"symbols": [], "rateLimits": []}),
    })
    assert client.exchange_info() == {"symbols": [], "rateLimits": []}
    assert client.base_url == "https://fapi.binance.com"
    assert seen == ["https://www.binance.com/fapi/v1/exchangeInfo", "https://fapi.binance.com/fapi/v1/exchangeInfo"]


def test_client_fatal_when_every_address_fails():
    client, _ = client_with({
        "https://www.binance.com": FakeResponse(451, {}),
        "https://fapi.binance.com": FakeResponse(451, {}),
    })
    with pytest.raises(BinanceFatalError):
        client.exchange_info()
