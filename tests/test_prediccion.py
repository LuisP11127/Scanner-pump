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


def test_explosiones_y_resultado_a_10_dias():
    t, v = serie(40, explota=20, sube=0.4)
    ex = {e["apertura"]: e for e in P.explosiones(t, v)}
    e = ex[T0 + 20 * DIA]
    assert e["escaneo"] == T0 + 21 * DIA and e["pct_rango"] >= 25
    assert e["max_despues"] > 35  # el máximo de los 10 días siguientes frente al cierre de la vela
    # Sin los 10 días cerrados, sin resultado
    corto = t < T0 + 25 * DIA
    assert P.explosiones(t[corto], v[corto])[-1]["max_despues"] is None


def test_biblioteca_puntua_por_las_vecinas():
    rng = np.random.default_rng(1)
    x = np.vstack([rng.normal(0, 1, (200, 4)), rng.normal(5, 1, (200, 4))]).astype(np.float32)
    llego = np.r_[np.zeros(200), np.ones(200)]
    p, idx = P.Biblioteca(x, llego).puntuar(np.array([[5, 5, 5, 5], [0, 0, 0, 0]], np.float32), k=20)
    assert p[0] > 0.9 and p[1] < 0.1 and idx.shape == (2, 20)


def test_combinar_da_la_mitad_a_cada_cosa():
    s = P.combinar(np.array([0.1, 0.3, 0.2]), np.array([50.0, 10.0, 30.0]))
    assert np.allclose(s, [0.5, 0.5, 0.5])
    assert np.allclose(P.combinar(np.array([0.1, 0.3]), np.array([10.0, 50.0])), [0, 1])


def test_predecir_dia_resultados_y_web(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "VECINAS", 3)  # la biblioteca de prueba solo tiene unas pocas explosiones
    cache = tmp_path / "cache"
    for i, simbolo in enumerate(["AUSDT", "BUSDT", "CUSDT"]):
        t, v = serie(140, explota=100 + i, sube=0.6 if i == 0 else 0.0, semilla=i)
        (cache / "futures").mkdir(parents=True, exist_ok=True)
        np.savez_compressed(cache / "futures" / f"{simbolo}.npz", t=t, ohlcv=v.astype(np.float32))
    filas, x = P.construir_biblioteca(cache)
    assert len(filas) == len(x) and all(f["max_despues"] is not None for f in filas)
    apertura = T0 + 120 * DIA
    dia = {"fecha": "2023-12-31", "monedas": [
        {"mercado": "futures", "simbolo": "AUSDT", "base": "A", "apertura": apertura, "pct_rango": 30.0, "pct_cuerpo": 12.0},
        {"mercado": "futures", "simbolo": "BUSDT", "base": "B", "apertura": apertura, "pct_rango": 12.0, "pct_cuerpo": 2.0},
        {"mercado": "spot", "simbolo": "NUEVAUSDT", "base": "NUEVA", "apertura": apertura, "pct_rango": 40.0, "pct_cuerpo": 40.0}]}
    pred = P.predecir_dia(dia, cache, filas, x)
    a, b, nueva = pred["monedas"]
    assert nueva["parecido"] is None and nueva["puntuacion"] == {}  # sin velas: sin huella
    assert 0 <= a["parecido"] <= 1 and len(a["vecinas"]) == 3
    assert set(a["puntuacion"]) == {"rango", "cuerpo"} and set(b["puntuacion"]) == {"rango"}
    # La biblioteca solo tiene explosiones con resultado conocido antes del escaneo
    assert pred["biblioteca"]["explosiones"] == sum(f["escaneo"] + 10 * DIA <= apertura + DIA for f in filas)
    # Resultado a 10 días
    clave = {(f["mercado"], f["simbolo"], f["escaneo"]): f for f in filas}
    clave[("futures", "AUSDT", apertura + DIA)] = {"max_despues": 61.234}
    assert P.completar_resultados(pred, clave) and a["max_despues"] == 61.23
    assert not P.completar_resultados(pred, clave)
    # La web publica el día y el índice
    salida = tmp_path / "datos"
    salida.mkdir()
    assert P.reconstruida(pred)  # calculada hoy para un escaneo de 2023
    pred["calculada"] = pred["escaneo_utc"].replace("00:00:00", "03:00:00")  # como si se hubiera calculado ese día
    assert not P.reconstruida(pred) and P.reconstruida({"escaneo_utc": pred["escaneo_utc"]})
    assert not P.reconstruida({"escaneo_utc": "2026-11-09T00:00:00Z"})  # del workflow diario, sin la hora
    (salida / "2023-12-31.json").write_text(json.dumps(pred), encoding="utf-8")
    (salida / "prueba.json").write_text(json.dumps({"desde": "2024-01-01", "grupos": {}}), encoding="utf-8")
    web.write_predictions(salida, tmp_path / "site" / "prediccion", "x")
    indice = json.loads((tmp_path / "site" / "prediccion" / "indice.json").read_text(encoding="utf-8"))
    dia_ = indice["dias"][0]
    assert (dia_["fecha"], dia_["monedas"], dia_["con_resultado"]) == ("2023-12-31", 3, 1) and dia_["reconstruida"] is False
    assert dia_["grupos"]["rango"]["top5"] == {"llegaron": 1, "de": 1} and dia_["grupos"]["rango"]["monedas"] == 2
    assert indice["en_vivo"] == {"reales": {}, "reconstruidas": {}}  # en_vivo pide al menos 5 monedas con resultado
    assert indice["prueba"]["desde"] == "2024-01-01" and (tmp_path / "site" / "prediccion" / "2023-12-31.json").exists()


def test_en_vivo_y_prueba():
    monedas = [{"pct_rango": 10.0 + i, "pct_cuerpo": 1.0, "puntuacion": {"rango": i / 9}, "max_despues": 60.0 if i >= 8 else 5.0}
               for i in range(10)]
    vivo = P.en_vivo([{"monedas": monedas}])
    assert vivo["rango"]["base"] == 0.2 and vivo["rango"]["top5"] == 0.4 and vivo["rango"]["mejor_del_dia"] == 1.0
    assert "cuerpo" not in vivo
    rng = np.random.default_rng(3)
    n = 3000
    escaneo = 1_704_067_200_000 - 400 * DIA + np.sort(rng.integers(0, 500, n)) * DIA
    x = rng.normal(size=(n, 3)).astype(np.float32)
    filas = [{"escaneo": int(e), "pct_rango": 12.0 + float(xi[0]), "pct_cuerpo": 0.0, "max_despues": 80.0 if xi[0] > 1 else 0.0}
             for e, xi in zip(escaneo, x)]
    res = P.probar(filas, x)
    g = res["grupos"]["rango"]
    assert g["top5"]["combinada"] > g["base"] and g["top5"]["huella"] > g["base"] and "cuerpo" not in res["grupos"]
