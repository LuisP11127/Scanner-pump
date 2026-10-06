# Seguimiento

Un archivo por vela diaria con explosiones (`AAAA-MM-DD.json`, con la fecha con que Binance muestra la vela
en hora Lima): las monedas cuya vela subió un 10 % o más (de un mínimo a un máximo posterior, o de apertura a
cierre), con su **fecha de análisis** (la vela anterior) y la hora UTC del mínimo y del máximo.

Los escribe cada día el workflow **Escaneo diario** y también la web, si está conectada con GitHub
(**Seguimiento → Guardar también en GitHub**). El workflow **Web** los une en `seguimiento.json` al publicar
la página. Para borrar un día, usa el botón **Borrar** de la web o elimina su archivo.
