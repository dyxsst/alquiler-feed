# alquiler-feed

Datos y cargador diario del tracker «Búsqueda de alquiler».

- `feed/e24.json`, `feed/mitula.json`: anuncios de Encuentra24 y Mitula que tu computadora lee cada mañana
  (esos dos sitios bloquean la lectura desde la nube). Los escribe `vm/alquiler_vm.py`.
- `loader/`: el cargador que corre la tarea programada de Claude cada día. Lee Rent-A-House, Panama Real Estate,
  InmoPanamá y MLS ACOBIR directamente, toma Encuentra24 y Mitula de `feed/`, y escribe en el tracker
  solo anuncios nuevos y datos mejores de los existentes. Nunca toca tus estados, notas ni clasificaciones,
  nunca borra anuncios, y lo que eliminaste para siempre no vuelve.

## En tu computadora (VM)

El script vive en `~/Apto/alquiler_vm.py` y este repositorio en `~/Apto/alquiler-feed`.
Para actualizar el script: `cd ~/Apto/alquiler-feed && git pull && cp vm/alquiler_vm.py ~/Apto/`.

Corrida diaria con cron (`crontab -e`), antes de las 6:45 a. m. de Panamá:

    30 5 * * * cd $HOME/Apto && .venv/bin/python alquiler_vm.py >> alquiler_cron.log 2>&1

(si la VM está en hora UTC, usa `30 10 * * *`). El registro detallado queda en `~/Apto/alquiler_vm.log`.
