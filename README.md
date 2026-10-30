# alquiler-feed

Datos y código de la carga diaria del tracker «Búsqueda de alquiler».

Cómo funciona cada día:

1. **5:30 a. m., tu computadora (VM)** corre `vm/alquiler_vm.py` (copiado en `~/Apto/alquiler_vm.py`):
   - trae la última versión de este repositorio (`git pull`);
   - lee Encuentra24 y Mitula con un navegador (esos dos sitios bloquean la lectura desde la nube) → `feed/e24.json`, `feed/mitula.json`;
   - lee Rent-A-House, Panama Real Estate, InmoPanamá y MLS ACOBIR y normaliza las 6 fuentes con `loader/`
     (zona, distancia a Villa Lucre, servicios, solo venta, conflictos de ubicación…) → `feed/norm.json`;
   - sube todo aquí.
2. **6:45 a. m., la tarea programada de Claude** clona este repositorio solo para leer `feed/norm.json` y lo incorpora
   al tracker con el script que va escrito en las instrucciones de la tarea (copia de referencia en `tools/merge.py`).
   No ejecuta código de este repositorio. Nunca toca tus estados, notas ni clasificaciones, nunca borra anuncios,
   y lo que eliminaste para siempre no vuelve.

Si una fuente falla, el archivo lo dice para esa fuente y las demás siguen.

## En la VM

    cd ~/Apto/alquiler-feed && git pull && cp vm/alquiler_vm.py ~/Apto/

Cron (`crontab -e`), antes de las 6:45 a. m. de Panamá:

    30 5 * * * cd $HOME/Apto && .venv/bin/python alquiler_vm.py >> alquiler_cron.log 2>&1

(si la VM está en hora UTC, usa `30 10 * * *`). Registro de cada corrida: `~/Apto/alquiler_cron.log`.
Las fichas ya leídas de los 4 sitios quedan en `~/Apto/alquiler_cache.json` para no reabrirlas cada día.
