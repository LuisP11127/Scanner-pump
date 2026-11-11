import json

import numpy as np

from scanner_pump import prediccion as P
from scanner_pump import web

DIA = P.DIA_MS
T0 = 1_700_006_400_000 - 1_700_006_400_000 % DIA  # medianoche UTC


def serie(dias, explota=None, sube=0.0, semilla=0):
    """Velas de 2H con precio casi plano; ``explota``: índice del día con una vela de +30 %, tras la que sube ``sube``."""
    rng = np.random.default_rng(semilla)
    t = T0 + np.arange(dias * 12, dtype=np.int64) * P.PASO_2H
    precio = 1 + 0.01 * np.cumsum(rng.normal(size=len(t)))
    precio = np.abs(precio) + 0.5
    if explota is not None:
        i = explota * 12
        precio[i:i + 12] *= np.linspace(1, 1.3, 12)
        precio[i + 12:] *= 1.3 * (1 + sube)
    o = np.r_[precio[0], precio[:-1]]
    v = np.column_stack([o, np.maximum(o, precio) * 1.002, np.minimum(o, precio) * 0.998, precio, rng.uniform(1, 2, len(t))])
    return t, v


def test_agrupar_alinea_con_las_0_utc():
    t, v = serie(3)
    td, vd = P.agrupar(t, v, 12)
    assert len(td) == 3 and (td % DIA == 0).all()
    assert vd[0, 0] == v[0, 0] and vd[0, 3] == v[11, 3]
    assert vd[0, 1] == v[:12, 1].max() and vd[0, 2] == v[:12, 2].min() and np.isclose(vd[0, 4], v[:12, 4].sum())
    t4, v4 = P.agrupar(t, v, 2)
    assert len(t4) == 18 and (t4 % (2 * P.PASO_2H) == 0).all()


def test_fractales_de_5_velas():
    h = np.array([1, 2, 5, 2, 1, 1, 3, 1, 1.0])
    l = np.array([1, 1, 1, 1, 0.2, 1, 1, 1, 1.0])
    techos, suelos = P._fractales(h, l)
    assert techos.tolist() == [2, 6] and suelos.tolist() == [4]


def test_huella_solo_con_velas_anteriores_al_escaneo():
    t, v = serie(60)
    escaneo = T0 + 50 * DIA
    h = P.huella(t, v, escaneo)
    assert h is not None and h.shape == (len(P.nombres()),)
    # Cambiar lo que pasa después del escaneo no cambia la huella
    v2 = v.copy()
    v2[t >= escaneo, :4] *= 3
    assert np.array_equal(P.huella(t, v2, escaneo), h)
    # Sin bastantes velas de 12H (moneda nueva), no hay huella
    assert P.huella(t, v, T0 + 10 * DIA) is None


def test_acierto_es_explotar_y_subir_50():
    assert P.acierto([12, 12, 3, np.nan], [60, 20, 80, 90]).tolist() == [True, False, False, False]


def test_vela_de_analisis_y_lo_que_paso_despues():
    t, v = serie(40, explota=20, sube=0.4)
    a = P.vela_analisis(t, v, T0 + 20 * DIA)  # la vela del día 19 cierra cuando abre la explosión
    assert a["apertura"] == T0 + 19 * DIA and a["pct_rango"] < 10
    assert a["manana"] >= 25  # la vela siguiente es la explosión
    assert a["max_despues"] > 60  # máximo de los 10 días siguientes frente al cierre
    corto = t < T0 + 25 * DIA  # sin los 10 días cerrados, sin máximo; la vela siguiente sí
    b = P.vela_analisis(t[corto], v[corto], T0 + 20 * DIA)
    assert b["max_despues"] is None and b["manana"] == a["manana"]
    assert P.vela_analisis(t, v, T0 + 60 * DIA) is None  # el par no cotizaba


def test_biblioteca_puntua_por_las_vecinas():
    rng = np.random.default_rng(1)
    x = np.vstack([rng.normal(0, 1, (200, 4)), rng.normal(5, 1, (200, 4))]).astype(np.float32)
    llego = np.r_[np.zeros(200), np.ones(200)]
    p, idx = P.Biblioteca(x, llego).puntuar(np.array([[5, 5, 5, 5], [0, 0, 0, 0]], np.float32), k=20)
    assert p[0] > 0.9 and p[1] < 0.1 and idx.shape == (2, 20)
    # Las que no subieron pesan más (en la biblioteca solo está 1 de cada 4)
    mitad = np.vstack([np.zeros((2, 4)), np.zeros((2, 4))]).astype(np.float32)
    p, _ = P.Biblioteca(mitad, np.array([1, 1, 0, 0]), np.array([1.0, 1.0, 4.0, 4.0])).puntuar(np.zeros((1, 4), np.float32), k=4)
    assert np.isclose(p[0], 0.2)


def _cache(tmp_path):
    cache = tmp_path / "cache"
    for i, (mercado, simbolo, explota) in enumerate([("futures", "AUSDT", 100), ("spot", "AUSDT", 100), ("futures", "BUSDT", 70),
                                                     ("futures", "CUSDT", None), ("spot", "DUSDT", 119)]):
        t, v = serie(140, explota=explota, sube=0.6, semilla=i)
        (cache / mercado).mkdir(parents=True, exist_ok=True)
        np.savez_compressed(cache / mercado / f"{simbolo}.npz", t=t, ohlcv=v.astype(np.float32))
    return cache


def test_predecir_y_completar(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "VECINAS", 3)
    cache = _cache(tmp_path)
    rejilla = P.construir_rejilla(cache)
    assert len(rejilla) and (rejilla.dia % (2 * DIA) == P.DESDE_REJILLA % (2 * DIA)).all()
    dia = T0 + 120 * DIA  # cierre de la vela del día 119: DUSDT explota en ella (no es candidata)
    pred = P.predecir(cache, dia, rejilla, ahora=dia + 3600_000)
    assert pred["version"] == P.VERSION and pred["escaneo_utc"].startswith(P._iso(dia)[:10]) and not P.reconstruida(pred)
    assert pred["fecha"] == P.etiqueta(dia)
    simbolos = [c["simbolo"] for c in pred["monedas"]]
    assert "DUSDT" not in simbolos and simbolos.count("AUSDT") == 1  # en spot y futuros: una vez
    assert pred["evaluadas"] == len(pred["monedas"]) == 3
    assert all(c["pct_rango"] < P.QUIETA and 0 <= c["parecido"] <= 1 and c["fecha"] == pred["fecha"] for c in pred["monedas"])
    assert [c["parecido"] for c in pred["monedas"]] == sorted((c["parecido"] for c in pred["monedas"]), reverse=True)
    # La biblioteca solo tiene situaciones cuyo resultado ya se conocía
    assert pred["biblioteca"]["situaciones"] <= ((rejilla.dia + 10 * DIA <= dia) & np.isfinite(rejilla.max10)).sum()
    # Lo que pasó después se añade cuando se sabe
    assert P.completar(pred, cache) and all(c["manana"] is not None and c["max_despues"] is not None for c in pred["monedas"])
    assert pred["base"]["monedas"] == 3 and pred["base"]["llegaron"] is not None and pred["base"]["acertaron"] is not None
    assert not P.completar(pred, cache)
    # Muy reciente: ni vela siguiente ni 10 días
    tarde = P.predecir(cache, T0 + 139 * DIA, rejilla, ahora=T0 + 139 * DIA)
    assert not P.completar(tarde, cache) or all(c.get("max_despues") is None for c in tarde["monedas"])


def test_resumen_en_vivo_y_web(tmp_path):
    monedas = [{"mercado": "spot", "simbolo": f"X{i}USDT", "manana": 12.0 if i < 2 else 3.0, "max_despues": {0: 60.0, 2: 70.0}.get(i, 5.0)}
               for i in range(6)]
    pred = {"version": P.VERSION, "fecha": "2026-11-07", "escaneo_utc": "2026-11-09T00:00:00Z", "calculada": "2026-11-09T02:00:00Z",
            "evaluadas": 300, "monedas": monedas, "base": {"monedas": 300, "explotaron": 0.1, "llegaron": 0.04, "acertaron": 0.01}}
    # X0 explotó y subió +50 % (acierta); X2 subió +50 % pero sin explotar al día siguiente (no acierta)
    assert P.resumen_dia(pred) == {"explotaron": 2, "de_manana": 5, "llegaron": 2, "acertaron": 1, "de_10d": 5}
    vivo = P.en_vivo([pred])
    assert (vivo["explotaron"], vivo["de_manana"], vivo["llegaron"], vivo["acertaron"], vivo["de_10d"]) == (2, 5, 2, 1, 5)
    assert vivo["base_explotaron"] == 0.1 and vivo["base_llegaron"] == 0.04 and vivo["base_acertaron"] == 0.01
    vieja = dict(pred, calculada="2026-11-12T00:00:00Z", fecha="2026-11-06", escaneo_utc="2026-11-08T00:00:00Z")
    assert P.reconstruida(vieja) and not P.reconstruida(pred)
    salida = tmp_path / "datos"
    salida.mkdir()
    (salida / "2026-11-07.json").write_text(json.dumps(pred), encoding="utf-8")
    (salida / "2026-11-06.json").write_text(json.dumps(vieja), encoding="utf-8")
    (salida / "2026-11-05.json").write_text(json.dumps({"fecha": "2026-11-05", "monedas": []}), encoding="utf-8")  # versión anterior
    (salida / "prueba.json").write_text(json.dumps({"desde": "2025-01-01"}), encoding="utf-8")
    web.write_predictions(salida, tmp_path / "site", "x")
    indice = json.loads((tmp_path / "site" / "indice.json").read_text(encoding="utf-8"))
    assert [d["fecha"] for d in indice["dias"]] == ["2026-11-07", "2026-11-06"]
    assert indice["dias"][0]["top5"]["explotaron"] == 2 and indice["dias"][1]["reconstruida"] is True
    assert indice["en_vivo"]["reales"]["dias_manana"] == 1 and indice["prueba"]["desde"] == "2025-01-01"
    assert not (tmp_path / "site" / "2026-11-05.json").exists()


def test_probar_sin_mirar_el_futuro(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "VECINAS", 3)
    rejilla = P.construir_rejilla(_cache(tmp_path))
    res = P.probar(rejilla, desde="2024-01-01")
    assert res["dias"] > 0 and 0 <= res["top5"]["explotaron"] <= 1 and 0 <= res["base"]["llegaron"] <= 1
    assert res["base"]["acertaron"] <= min(res["base"]["llegaron"], res["base"]["explotaron"])
