# Scanner-pump

Capta las velas diarias de Binance (**spot** y **futuros USDⓈ-M perpetuos**, pares en USDT) que subieron
**un 10 % o más**. Cada vela se mide de dos formas y entra en la lista si cumple cualquiera de ellas:

| Medida | Cálculo | Ejemplo |
|---|---|---|
| **Mín→Máx** | `(máximo − mínimo) / mínimo` | mínimo 1.00, máximo 1.25 → **+25 %** |
| **Apertura→Cierre** | `(cierre − apertura) / apertura` | apertura 1.00, cierre 1.12 → **+12 %** |

Toda vela con Apertura→Cierre ≥ 10 % tiene también Mín→Máx ≥ 10 % (el mínimo nunca está por encima de la
apertura ni el máximo por debajo del cierre), así que la lista son las velas con Mín→Máx ≥ 10 % y de cada una
se indica si además cumple Apertura→Cierre (etiqueta **≥10%**).

Datos (API pública, sin claves):

- **Spot**: `https://data-api.binance.vision` (si falla, `api.binance.com`).
- **Futuros**: `https://www.binance.com` (si rechaza la conexión, `fapi.binance.com`).

## Fechas: velas de 00:00 UTC en hora Lima

Binance abre cada vela diaria a las **00:00 UTC**, que en Lima (UTC-5) son las **19:00 del día anterior**, y la
muestra con la fecha de Lima. Por eso la vela «del 2 de octubre» empieza el 2 oct a las 19:00 y **termina de
construirse el 3 oct a las 19:00** (hora Lima). Todas las fechas del scanner siguen ese criterio.

Ejemplo: el **3 de octubre a las 00:17** (hora Lima) todavía se está construyendo la vela del 2 de octubre, así
que el scanner toma **la última vela completa: la del 1 de octubre**. Si esa vela subió un 10 % o más, su
**fecha de análisis** es la vela anterior, **30 de septiembre**: el día que conviene estudiar para ver qué
anunciaba la explosión.

## La web: Escáner, Monedas listadas 2026, Seguimiento y Predicción

`python -m scanner_pump.web site` genera la página 🚀 (la publica sola el workflow **Web**). Tiene cuatro
pestañas: **Escáner**, **Monedas listadas 2026**, **Seguimiento** y **Predicción**. Las peticiones a Binance salen desde tu
navegador, así que no hace falta ningún servidor.

**Gráficos con todo el historial.** Cada gráfico pide a Binance el máximo de velas por petición (1000) y, al
abrir el visor, sigue descargando hacia atrás, página a página y sin mover lo que estás viendo, hasta el listado
de la moneda (con un tope de 60 000 velas por gráfico). Debajo de la leyenda se ve cuántas hay: «Historial
completo: 40 161 velas de 2H desde el 16 ago 2017». El botón ⛶ (o la tecla `0`) muestra el historial entero.

### Escáner

- **Escanear** busca en spot y futuros la última vela diaria completa (o la fecha que elijas en **Vela 1D**).
  Con **Guardar en seguimiento** marcado, el resultado se guarda también en la pestaña Seguimiento.
- Al abrir la página se muestran las monedas del último día guardado en el seguimiento (el escaneo automático
  de cada día), sin tener que escanear.
- Filtros: **Todos / Spot / Futuros / No repetidas** (cada moneda una vez; si está en los dos mercados, la de
  spot), **Medida** (Mín→Máx o Apertura→Cierre), **Subida mín.** (≥ 10, 15, 20, 25, 30, 40, 50, 75 o 100 %),
  orden por mayor subida, volumen o símbolo, y búsqueda.
- Cada tarjeta trae su gráfico con **MA(7)**, **MA(25)** y **MA(99)** (colores de Binance), la vela de la
  explosión marcada en naranja y la de análisis en azul. **Gráfico 2H · 4H · 8H · 12H · 1D** cambia la
  temporalidad y **Tamaño S / M / L** agranda o achica las tarjetas.
- Al pulsar una tarjeta se abre el gráfico a pantalla completa:
  - **Siguiente** (o `→`) pasa a la siguiente criptomoneda, `←` a la anterior y `Esc` cierra;
  - `−` / `+` (o la rueda del ratón / pellizco) aleja y acerca, ⛶ (o `0`) muestra todo el historial y ◎ (o
    `E`) vuelve a la explosión;
  - **Hasta el análisis** (o `H`) oculta las velas posteriores: el gráfico tal como estaba al cerrar la fecha de
    análisis, para estudiarlo sin ver lo que pasó después;
  - las barras de volumen llevan encima sus medias **MA(5)** y **MA(25)**, como en Binance y Momento-A;
  - al pasar el ratón se ven apertura, máximo, mínimo, cierre, las dos subidas, las tres medias y el volumen de
    esa vela con sus dos medias.
- **Cargar informe** abre un `.html` o `.json` generado por la línea de comandos o por el workflow.

### Monedas listadas 2026

Todas las monedas que Binance listó en 2026, en spot y en futuros (pares en USDT), hayan subido o no,
**agrupadas por mes de listado** (el mes más reciente primero, o de enero en adelante). De cada una: la fecha y
hora de listado (hora Lima), el precio inicial, el precio actual, la variación desde el listado y sus
explosiones en el seguimiento (cuántas y la última). Cada mes resume cuántas hay en spot y en futuros, la
variación media y la mejor. Filtros **Todos / Spot / Futuros / No repetidas**, búsqueda y orden dentro del mes
(fecha de listado, mayor subida o moneda). Al pulsar una moneda se abre su gráfico con todo el historial desde
el listado, marcado con «Listado».

- **Fecha de listado:** en futuros, la de lanzamiento del contrato (`onboardDate` de Binance); en spot, la
  primera vela del par. Las que ya no cotizan siguen en la lista, marcadas con **No cotiza**.
- **Precio inicial:** la apertura de la primera vela de 1 minuto.
- Las fechas no cambian, así que el escaneo diario las guarda en `datos/listados.json` (solo pide a Binance los
  pares nuevos) y la web las lee de ahí. Lo que todavía no esté en el archivo lo pide tu navegador una sola vez
  y lo guarda.

### Seguimiento

Un apartado por día con explosiones (**Vela del 1 de octubre**, **Vela del 30 de septiembre**…), con las
columnas **Fecha de explosión** y **Fecha de análisis** (la vela anterior), las dos subidas, el cierre de la
vela, el **mínimo y el máximo después del escaneo**, el precio actual y la variación desde el cierre (se
actualiza cada minuto).

**Mínimo y máximo después del escaneo:** el precio más bajo y el más alto que alcanzó la moneda desde el
escaneo, en cualquier orden, cada uno con su % respecto al cierre de la vela y la hora (UTC) a la que llegó.
Cuentan desde el **cierre de la vela de la explosión** (00:00 UTC = 19:00 hora Lima), que es cuando corre el
escaneo automático, así que el punto de partida es el mismo para los escaneos automáticos y los manuales. Se
calculan en tu navegador con las velas de Binance (de 5 minutos los primeros días; más grandes según pasa el
tiempo, para cubrirlo todo en una petición), se guardan en el navegador y en cada visita solo se piden las
velas nuevas. El orden **Mayor máximo después del escaneo** pone arriba las que más subieron. Tiene los mismos filtros que
el escáner y al pulsar una moneda se abre su gráfico con las dos velas marcadas.

Los días llegan de dos sitios:

1. **El escaneo automático diario** (GitHub Actions) los guarda en el repositorio.
2. **Tus escaneos desde la web** se guardan en el navegador. Para guardarlos también en el repositorio (y
   verlos igual en el móvil y en el ordenador), pulsa **Guardar también en GitHub** y pega una clave
   (*fine-grained token*) creada en <https://github.com/settings/personal-access-tokens/new> con
   **Repository access → Only select repositories →** este repositorio y **Permissions → Contents → Read and
   write**. La clave se guarda solo en ese navegador y solo se envía a GitHub.

Cada día es un archivo `datos/seguimiento/AAAA-MM-DD.json`; si el workflow y la web guardan el mismo día, se
unen (cada moneda una vez por mercado). **Borrar** quita un día.

Entre el 6 y el 7 de octubre de 2026 el escáner exigió que el mínimo de la vela fuera antes que el máximo. Si
queda alguna moneda guardada con esa medida, su Mín→Máx sale con un asterisco y el día muestra **Volver a
escanear**, que la sustituye por la medida del día completo.

### Predicción

Busca la **vela de análisis**: monedas que **todavía no explotaron** pero que tienen la huella que tenían, el día
antes de explotar, las monedas que luego subieron **+50 %**. Cada día, al cierre de la vela (19:00 hora Lima), se
revisan **todas** las monedas de Binance cuya vela del día subió menos de 10 % de mínimo a máximo (las que se
movieron menos de 5 % en el último mes, casi estables, no cuentan) y quedan las 30 más parecidas.

Es un **historial**, una predicción por día: «Predicción del 09-11-2026» (al cierre de la vela del 7 de noviembre
en hora Lima), la del 10-11-2026 al día siguiente, y así. Se ven las 10 primeras (con un botón para ver las 30) y,
de cada una, lo que pasó después:

- **Día siguiente:** la vela siguiente de mínimo a máximo; ✓ si explotó (+10 % o más).
- **Máx después / Máx en 10 días:** el precio más alto desde el cierre de la vela de análisis, en vivo mientras
  corren los 10 días; ✓ si llegó a +50 %.
- **Tasa de acierto** (arriba): de las 5 primeras de cada día, cuántas explotaron al día siguiente y cuántas
  llegaron a +50 % en 10 días, frente a la media de todas las monedas sin explotar de esos días. Las
  **predicciones del día** (calculadas al cierre, desde el 09-11-2026) se cuentan aparte de las **reconstruidas**
  (velas de análisis del 30 de septiembre al 6 de noviembre, calculadas después pero solo con lo que se sabía
  cada día); la que vale es la primera.

Cómo se calcula (`python -m scanner_pump.prediccion`, workflow **Predicción**; se guarda en
`datos/prediccion/AAAA-MM-DD.json` con la fecha de la vela de análisis):

- **La huella** de una moneda son sus velas de 2H, 4H, 8H y 12H hasta el cierre, 84 de cada una (7, 14, 28 y 42
  días): la forma del precio, cómo se reparte el volumen, la volatilidad, la tendencia y los **fractales** de 5
  velas (techos y suelos: cuántos hay, si van subiendo, la distancia al último y si ya rompió el último techo).
  Solo cuentan los fractales confirmados antes del cierre.
- **La biblioteca:** todas las monedas que hoy tiene Binance, cada 2 días desde 2023 (unas 400 000 situaciones),
  con lo que pasó después: si el máximo de los **10 días siguientes** subió **+50 %** sobre el cierre.
- **Parecidas que subieron:** de las 100 situaciones pasadas más parecidas (cuyo resultado ya se conocía ese día),
  qué parte subió +50 %. Cuentan también las que no subieron: una huella frecuente entre las que subieron pero
  también entre las que no, no puntúa alto. Al lado salen tres parecidas que subieron, con su vela de análisis.
- **¿Acierta?** En la prueba hacia atrás (mes a mes desde 2025, sin mirar el futuro, unas 180 000 velas de monedas que
  aún no habían explotado), de las 5 primeras de cada día explotaron al día siguiente el 39 % (media del día: 18 %)
  y llegaron a +50 % en 10 días el 9,3 % (media: 3,8 %): unas 2,5 veces más que al azar. No es una garantía: la mayoría de las monedas de la lista no llega a +50 %.

## GitHub Actions

| Workflow | Cuándo | Qué hace |
|---|---|---|
| **Escaneo diario** (`escaneo-diario.yml`) | Cada día a las 00:07 UTC (19:07 hora Lima), justo después del cierre de la vela. También a mano. | Escanea la última vela completa y guarda las explosiones en `datos/seguimiento/`. Actualiza las fechas de listado en `datos/listados.json` (solo los pares nuevos). Deja el informe con los gráficos en **Artifacts → scanner-pump** y la tabla en el resumen. |
| **Predicción** (`prediccion.yml`) | Cada día a las 00:25 UTC y tras cada escaneo diario. También a mano (se pueden elegir velas de análisis o repetir la prueba). | Pone al día las velas de 2H de todos los pares (en la caché de Actions, no en el repositorio; la primera vez descarga desde 2022, hasta unos 35 minutos), busca las monedas que aún no explotaron con huella de vela de análisis, guarda `datos/prediccion/` y añade lo que ya se sabe de las anteriores (vela siguiente y máximo a 10 días). |
| **Web** (`web.yml`) | Tras cada escaneo diario, cada predicción y cada cambio en `main`. | Publica la web en GitHub Pages con todo el seguimiento (`seguimiento.json`), las fechas de listado (`listados.json`) y las predicciones (`prediccion/`). |
| **Tests** (`tests.yml`) | En cada pull request y cambio en `main`. | Pasa los tests y comprueba la huella de la librería de gráficos. |

**Escaneo manual:** **Actions → Escaneo diario → Run workflow**. Puedes dejar la fecha vacía (última vela
completa) o escribir una o varias fechas separadas por espacios (`2026-10-01 2026-10-02`, fecha de la vela en
hora Lima) para rellenar o volver a escanear días pasados, elegir el mercado y si se guarda en el seguimiento.

### Puesta en marcha

1. Une esta rama a `main` (los workflows programados solo se ejecutan desde la rama principal).
2. **Settings → Pages → Build and deployment → Source: GitHub Actions** (GitHub Pages es gratis en
   repositorios públicos; en privados hace falta GitHub Pro).
3. **Actions → Escaneo diario → Run workflow** para el primer escaneo. Al terminar, el workflow **Web** publica
   la página en `https://<tu-usuario>.github.io/Scanner-pump/`.

Desde entonces se actualiza sola cada día a las 19:07 (hora Lima); GitHub puede retrasar los workflows
programados unos minutos.

### Futuros desde los servidores de GitHub

Binance bloquea muchas conexiones desde servidores en la nube. Para spot, `data-api.binance.vision` responde
desde GitHub Actions. Para futuros se usa `www.binance.com` y, si falla, `fapi.binance.com`; si Binance
rechaza los dos, el workflow guarda igualmente spot, avisa en el resumen («Escaneo incompleto») y puedes
completar los futuros de ese día escaneando desde la web (con la clave de GitHub conectada se suman al mismo
archivo).

## Línea de comandos

Requiere Python 3.10 o superior.

```bash
pip install -r requirements.txt
python -m scanner_pump                          # spot + futuros, última vela completa
python -m scanner_pump --fecha 2026-09-28       # otra vela (fecha en hora Lima)
python -m scanner_pump -m futures --min-pct 20  # solo futuros, subidas de 20 % o más
python -m scanner_pump --min-volumen 1000000 --orden cuerpo --top 20
python -m scanner_pump --seguimiento datos/seguimiento   # guarda el día en el seguimiento
python -m scanner_pump --csv velas.csv --json velas.json
python -m scanner_pump.listados datos/listados.json      # actualiza las fechas de listado
```

Al terminar abre en el navegador el informe con los gráficos (`reportes/scanner-pump_<fecha>.html`).

| Opción | Por defecto | Descripción |
|---|---|---|
| `-m, --mercado {spot,futures,ambos}` | `ambos` | Mercado a escanear. |
| `-f, --fecha AAAA-MM-DD` | última vela completa | Vela a analizar, con la fecha con que Binance la muestra en hora Lima. |
| `--min-pct N` | `10` | Subida mínima en %. |
| `--min-volumen N` | `0` | Volumen mínimo de la vela, en USDT. |
| `-o, --orden {rango,cuerpo,volumen,simbolo}` | `rango` | Orden de la tabla. |
| `--top N` | — | Solo las N primeras de cada mercado. |
| `--seguimiento CARPETA` | — | Guarda las explosiones en `CARPETA/AAAA-MM-DD.json`. |
| `--csv / --json / --html ARCHIVO` | — | Exporta los resultados (el JSON y el HTML incluyen las velas de los gráficos). |
| `--sin-grafico`, `--no-abrir` | no | No generar el informe / no abrirlo. |
| `--incluir-stables`, `--incluir-acciones` | no | No descartar stablecoins ni acciones tokenizadas (bStocks). |
| `--workers N` | `8` | Descargas en paralelo. |
| `--spot-url / --futures-url` | ver arriba | URL base alternativa de la API. |

## Qué pares se analizan

- **Spot**: pares en USDT con estado `TRADING`, sin stablecoins/fiat como base y sin las acciones
  tokenizadas de Binance (bStocks: AAPLB, NVDAB…).
- **Futuros**: contratos USDⓈ-M **perpetuos** en USDT, sin índices ni activos no cripto (oro, acciones…).
- Los pares que no tenían vela ese día (aún no cotizaban) no cuentan como analizados.

El scanner respeta el límite de peso por minuto de Binance y reintenta ante errores `429` o de red.

## Tests

```bash
pip install pytest
python -m pytest
```

No es una recomendación de inversión.
