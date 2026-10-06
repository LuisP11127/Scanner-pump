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
from scanner_pump.scanner import (CHART_BEFORE, CHART_LIMIT, INTRADAY, Candle, RunUp, ScanConfig, explosion_to_dict,
                                  fetch_charts, is_explosion, may_explode, pick_day, run_up, scan_market, select_symbols)
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

HOUR = 3_600_000
FIVE_MIN = 300_000


def k5(open_ms, o, hi, lo, c):
    return [open_ms, str(o), str(hi), str(lo), str(c), "10", open_ms + FIVE_MIN - 1, "10", 1, "0", "0", "0"]


def day_path(open_ms, waypoints, quote_volume=1_000_000.0):
    """Velas de 5 minutos que recorren en línea recta los puntos [(hora UTC, precio)] y la vela diaria que forman."""
    def price(t):
        h = (t - open_ms) / HOUR
        for (h0, p0), (h1, p1) in zip(waypoints, waypoints[1:]):
            if h0 <= h <= h1:
                return p0 + (p1 - p0) * (h - h0) / (h1 - h0)
        return waypoints[-1][1]

    rows = []
    for i in range(288):
        t = open_ms + i * FIVE_MIN
        o, c = price(t), price(t + FIVE_MIN)
        rows.append(k5(t, o, max(o, c), min(o, c), c))
    o, c = float(rows[0][1]), float(rows[-1][4])
    hi, lo = max(float(r[2]) for r in rows), min(float(r[3]) for r in rows)
    return kline(open_ms, o, hi, lo, c, quote_volume), rows


def test_candle_percentages():
    c = Candle(0, open=1.0, high=1.3, low=0.9, close=1.2, quote_volume=10)
    assert c.pct_extremes == pytest.approx((1.3 - 0.9) / 0.9 * 100)
    assert c.pct_cuerpo == pytest.approx(20.0)


def test_run_up_inside_one_candle():
    # Alcista: apertura → mínimo → máximo → cierre, así que cuenta del mínimo al máximo.
    assert run_up([k5(0, 1.0, 1.2, 0.9, 1.1)]).pct == pytest.approx((1.2 - 0.9) / 0.9 * 100)
    # Bajista: apertura → máximo → mínimo → cierre; el mínimo llega después del máximo.
    rise = run_up([k5(0, 1.0, 1.2, 0.9, 0.95)])
    assert rise.pct == pytest.approx(20.0) and rise.low == 1.0 and rise.high == 1.2


def test_run_up_user_example():
    # Máximo a las 09:00 UTC y mínimo a las 12:00: esa caída no cuenta. Sí la subida del mínimo de las 12:00
    # al máximo posterior de las 15:00.
    _, rows = day_path(DAY, [(0, 2.0), (9, 2.1), (12, 1.8), (15, 2.05), (24, 2.0)])
    rise = run_up(rows)
    assert rise.pct == pytest.approx((2.05 - 1.8) / 1.8 * 100)
    assert abs(rise.low_time - (DAY + 12 * HOUR)) <= FIVE_MIN
    assert abs(rise.high_time - (DAY + 15 * HOUR)) <= FIVE_MIN
    assert rise.low_time < rise.high_time


def test_run_up_order_matters():
    falling = [k5(i * FIVE_MIN, p, p, p, p) for i, p in enumerate([2.3, 2.2, 2.0, 1.9])]
    assert run_up(falling).pct == pytest.approx(0.0)
    rising = list(reversed([k5(i * FIVE_MIN, p, p, p, p) for i, p in enumerate(reversed([2.3, 2.2, 2.0, 1.9]))]))
    assert run_up(rising).pct == pytest.approx((2.3 - 1.9) / 1.9 * 100)  # se ordenan por hora


def test_is_explosion_by_rise_or_body():
    flat = Candle(0, 1.0, 1.2, 0.95, 1.0, 0)
    assert may_explode(flat) and not may_explode(Candle(0, 1.0, 1.05, 0.98, 0.99, 0))
    assert is_explosion(flat, RunUp(10.0, 1, 0, 1.1, 0))
    assert not is_explosion(flat, RunUp(9.99, 1, 0, 1.0999, 0))
    assert is_explosion(Candle(0, 1.0, 1.3, 1.0, 1.3, 0), None, min_pct=30)  # apertura→cierre
    assert not is_explosion(Candle(0, 1.0, 1.3, 1.0, 1.3, 0), RunUp(30.0, 1, 0, 1.3, 0), min_pct=31)


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
        {"symbol": "DDDUSDT", "status": "TRADING", "baseAsset": "DDD", "quoteAsset": "USDT"},
        {"symbol": "NEWUSDT", "status": "TRADING", "baseAsset": "NEW", "quoteAsset": "USDT"},
        {"symbol": "LOWUSDT", "status": "TRADING", "baseAsset": "LOW", "quoteAsset": "USDT"},
        {"symbol": "AAPLBUSDT", "status": "TRADING", "baseAsset": "AAPLB", "quoteAsset": "USDT"},
        {"symbol": "USDCUSDT", "status": "TRADING", "baseAsset": "USDC", "quoteAsset": "USDT"},
        {"symbol": "AAABTC", "status": "TRADING", "baseAsset": "AAA", "quoteAsset": "BTC"},
        {"symbol": "OLDUSDT", "status": "BREAK", "baseAsset": "OLD", "quoteAsset": "USDT"},
    ]
}

DAY = utc_ms(2026, 10, 2)  # vela del 1 oct (hora Lima)

# Recorridos del día (hora UTC, precio) y volumen de la vela.
PATHS = {
    "AAAUSDT": ([(0, 1.0), (6, 0.98), (18, 1.35), (24, 1.30)], 5e6),             # sube: +37.8 % / +30 %
    "BBBUSDT": ([(0, 2.25), (9, 2.3), (12, 1.9), (15, 2.0), (24, 1.95)], 2e6),   # rango 21 %, pero el máximo fue antes
    "CCCUSDT": ([(0, 3.0), (12, 3.1), (16, 2.95), (24, 3.05)], 1e6),             # 5 %: ni se mira
    "DDDUSDT": ([(0, 2.0), (9, 2.1), (12, 1.8), (15, 2.05), (24, 2.0)], 3e6),    # mínimo 12:00 → máximo 15:00: +13.9 %
    "LOWUSDT": ([(0, 1.0), (12, 1.5), (24, 1.4)], 1000),                         # poco volumen
    "AAPLBUSDT": ([(0, 1.0), (12, 1.5), (24, 1.4)], 5e6),                        # acción tokenizada
}


def market_data():
    """(velas diarias, velas de 5 minutos) por símbolo para el día DAY."""
    prev = DAY - DAY_MS
    daily, intraday = {"NEWUSDT": [kline(DAY + DAY_MS, 1, 3, 1, 2)]}, {}  # NEW: sin vela ese día
    for symbol, (waypoints, volume) in PATHS.items():
        candle, rows = day_path(DAY, waypoints, volume)
        start = float(candle[1])
        daily[symbol] = [kline(prev, start, start * 1.02, start * 0.98, start), candle]
        intraday[symbol] = rows
    return daily, intraday


class FakeClient:
    def __init__(self, market, exchange_info, data=None, charts=None, tags=None):
        self.market = market
        self.base_url = market.base_url
        self._info = exchange_info
        self._daily, self._intraday = data or ({}, {})
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
        if interval == INTRADAY:
            return [r for r in self._intraday.get(symbol, []) if start_time <= r[0] <= end_time][:limit]
        return self._charts.get((symbol, interval), [])


def spot_client(**kwargs):
    kwargs.setdefault("tags", {"AAPLBUSDT": ["bStocks"]})
    return FakeClient(SPOT, EXCHANGE_SPOT, market_data(), **kwargs)


def test_select_symbols_filters():
    spot = [s.symbol for s in select_symbols("spot", EXCHANGE_SPOT)]
    assert spot == ["AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT", "NEWUSDT", "LOWUSDT", "AAPLBUSDT"]
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
    client = spot_client()
    result = scan_market(client, ScanConfig(open_ms=DAY, min_quote_volume=10_000, workers=2))
    found = {e.symbol: e for e in result.explosions}
    assert set(found) == {"AAAUSDT", "DDDUSDT"}
    assert found["AAAUSDT"].pct_cuerpo == pytest.approx(30.0)
    assert found["AAAUSDT"].pct_rango == pytest.approx((1.35 - 0.98) / 0.98 * 100)
    assert found["DDDUSDT"].pct_rango == pytest.approx((2.05 - 1.8) / 1.8 * 100)
    assert found["DDDUSDT"].analysis.close == 2.0
    assert result.max_before_min == 1  # BBB: su rango llegaba al 21 %, pero el máximo fue antes del mínimo
    assert result.analyzed == 5  # AAA, BBB, CCC, DDD y LOW tenían vela ese día
    assert result.no_candle == 1
    assert result.skipped_volume == 1
    assert result.skipped_stocks == 1
    # Cada par pide dos velas diarias desde el día de análisis...
    assert ("AAAUSDT", "1d", 2, DAY - DAY_MS) in client.calls
    # ...y solo las candidatas, las de 5 minutos de ese día.
    intraday = {c[0] for c in client.calls if c[1] == INTRADAY}
    assert intraday == {"AAAUSDT", "BBBUSDT", "DDDUSDT"}


def test_scan_market_warns_without_stock_list():
    result = scan_market(spot_client(tags=None), ScanConfig(open_ms=DAY))
    assert "AAPLBUSDT" in {e.symbol for e in result.explosions}
    assert result.warnings


def test_scan_market_propagates_fatal_errors():
    class Blocked(FakeClient):
        def klines(self, *args, **kwargs):
            raise BinanceFatalError("451")

    info = {"symbols": [{"symbol": "XUSDT", "status": "TRADING", "baseAsset": "X", "quoteAsset": "USDT", "contractType": "PERPETUAL"}]}
    with pytest.raises(BinanceFatalError):
        scan_market(Blocked(FUTURES, info), ScanConfig(open_ms=DAY))


def test_fetch_charts_window_around_explosion():
    rows = [kline(DAY - 5 * 7_200_000, 1, 1, 1, 1)]
    client = spot_client(charts={("AAAUSDT", "2h"): rows})
    result = scan_market(client, ScanConfig(open_ms=DAY))
    fetch_charts(client, result, ["2h"])
    assert result.charts["AAAUSDT"]["2h"] == [[rows[0][0], 1.0, 1.0, 1.0, 1.0, 1000.0]]
    assert ("AAAUSDT", "2h", CHART_LIMIT, DAY - CHART_BEFORE * 7_200_000) in client.calls


def test_explosion_to_dict():
    result = scan_market(spot_client(), ScanConfig(open_ms=DAY))
    data = explosion_to_dict(next(e for e in result.explosions if e.symbol == "DDDUSDT"))
    assert data["fecha"] == "2026-10-01"
    assert data["fecha_analisis"] == "2026-09-30"
    assert data["apertura"] == DAY
    assert data["pct_rango"] == pytest.approx((2.05 - 1.8) / 1.8 * 100, abs=1e-3)
    assert data["min_precio"] == pytest.approx(1.8) and data["max_precio"] == pytest.approx(2.05)
    assert data["min_hora"] < data["max_hora"]
    assert data["intradia"] == INTRADAY
    assert data["analisis"]["close"] == 2.0
    assert data["volumen"] == 3e6


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


def test_merge_replaces_legacy_coins_of_rescanned_market(tmp_path):
    # Guardadas con el criterio antiguo (sin «intradia»): mínimo y máximo del día sin mirar el orden.
    write_day(tmp_path, date(2026, 10, 1), [coin("BBBUSDT", 21.0), coin("XXXUSDT", 15.0, "futures")])
    # Nuevo escaneo de spot: BBB ya no cuenta (su máximo fue antes del mínimo) y entra DDD.
    path = write_day(tmp_path, date(2026, 10, 1), [coin("DDDUSDT", 13.9, intradia="5m")])
    day = read_day(path)
    assert {(c["mercado"], c["simbolo"]) for c in day["monedas"]} == {("spot", "DDDUSDT"), ("futures", "XXXUSDT")}
    # Un guardado antiguo que llegue después tampoco vuelve a meter monedas del criterio antiguo en spot.
    path = write_day(tmp_path, date(2026, 10, 1), [coin("BBBUSDT", 21.0)])
    assert {c["simbolo"] for c in read_day(path)["monedas"]} == {"DDDUSDT", "XXXUSDT"}


# ---------- informe y web ----------

def test_render_page_embeds_payload_safely():
    html = render_page({"mercados": {}, "nota": "</script><b>"})
    assert DATA_MARKER not in html
    assert "</script><b>" not in html
    assert '"\\u003c/script>\\u003cb>"' in html
    assert TEMPLATE_PATH.read_text(encoding="utf-8").count(DATA_MARKER) == 1


def test_build_payload():
    result = scan_market(spot_client(), ScanConfig(open_ms=DAY))
    result.charts["AAAUSDT"] = {"1d": [[DAY, 1, 2, 1, 2, 5]]}
    payload = build_payload([result], datetime(2026, 10, 3, tzinfo=timezone.utc))
    assert payload["fecha"] == "2026-10-01" and payload["fecha_analisis"] == "2026-09-30"
    coins = payload["mercados"]["spot"]["coincidencias"]
    assert [c["pct_rango"] for c in coins] == sorted((c["pct_rango"] for c in coins), reverse=True)
    by_symbol = {c["simbolo"]: c for c in coins}
    assert by_symbol["AAAUSDT"]["graficos"]["1d"] and "graficos" not in by_symbol["DDDUSDT"]
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
        "spot": spot_client(),
        "futures": FakeClient(FUTURES, {"symbols": []}),
    }
    monkeypatch.setattr(cli, "BinanceClient", lambda market, **kw: clients[market.key])
    code = cli.main(["--fecha", "2026-10-01", "--seguimiento", str(tmp_path / "seg"), "--json", str(tmp_path / "r.json"),
                     "--html", str(tmp_path / "r.html"), "--no-abrir"])
    out = capsys.readouterr().out
    assert code == 0
    assert "vela del 1 de octubre de 2026" in out
    assert "AAAUSDT" in out and "DDDUSDT" in out and "BBBUSDT" not in out
    assert "1 descartadas porque el máximo fue antes del mínimo" in out
    assert re.search(r"DDDUSDT .* 1[12]:[0-9]{2}→1[45]:[0-9]{2} ", out)
    day = read_day(tmp_path / "seg" / "2026-10-01.json")
    assert {c["simbolo"] for c in day["monedas"]} == {"AAAUSDT", "DDDUSDT", "LOWUSDT"}  # sin --min-volumen
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

    clients = {"spot": spot_client(), "futures": Blocked(FUTURES, {})}
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


@pytest.mark.skipif(not shutil.which("node"), reason="hace falta Node.js")
def test_page_run_up_matches_python():
    import random

    script = page_script()
    fn = re.search(r"\n  function runUp\(rows\).*?\n  }\n", script, re.S).group(0)
    rnd = random.Random(7)
    cases = []
    for _ in range(40):
        price, rows = 1.0, []
        for i in range(rnd.randint(1, 60)):
            o = price
            c = max(0.01, o * (1 + rnd.uniform(-0.05, 0.05)))
            rows.append([i * FIVE_MIN, o, max(o, c) * (1 + rnd.uniform(0, 0.03)), min(o, c) * (1 - rnd.uniform(0, 0.03)), c])
            price = c
        rnd.shuffle(rows)  # la función ordena por hora
        cases.append(rows)
    js = fn + f"console.log(JSON.stringify({json.dumps(cases)}.map(runUp)));"
    result = json.loads(subprocess.run(["node", "-e", js], check=True, capture_output=True, text=True).stdout)
    for rows, got in zip(cases, result):
        want = run_up(rows)
        assert got["pct"] == pytest.approx(want.pct)
        assert (got["lowTime"], got["highTime"]) == (want.low_time, want.high_time)


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
