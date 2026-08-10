#!/usr/bin/env bash
# Corrida automatizada. Pensado para cron: sin interacción, todo a un log.
#
#   crontab -e
#   0 7 * * * /home/joaco/Carga_Datos_Arca/scripts/sincronizar.sh
#
# Cron corre con un entorno mínimo, así que todas las rutas van absolutas y el
# virtualenv se activa a mano.
set -uo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$RAIZ" || exit 1

LOGS="$RAIZ/data/logs"
mkdir -p "$LOGS"
LOG="$LOGS/sync_$(date +%Y%m%d).log"

# Un solo proceso a la vez: una corrida lenta no puede pisar a la siguiente.
CANDADO="$RAIZ/data/.sync.lock"
exec 9>"$CANDADO"
if ! flock -n 9; then
    echo "$(date -Is) ya hay una sincronización corriendo, se saltea" >> "$LOG"
    exit 0
fi

{
    echo "=============================================================="
    echo "$(date -Is)  inicio"
} >> "$LOG"

"$RAIZ/.venv/bin/python" main.py sync >> "$LOG" 2>&1
ESTADO=$?

echo "$(date -Is)  fin (estado $ESTADO)" >> "$LOG"

# Los logs viejos se van solos.
find "$LOGS" -name 'sync_*.log' -mtime +60 -delete 2>/dev/null

exit $ESTADO
