#!/usr/bin/env bash
# sync_bridge.sh — Copia el puente (y las utilidades compartidas) de ESTE repo de
# documentacion al repo del add-on de Home Assistant, para que no se desfasen.
#
# Los dos repos comparten conga_mqtt_bridge.py; este es la "fuente de la verdad".
# Tras un cambio aqui, ejecuta este script para llevarlo al add-on.
#
# Uso:
#   ./sync_bridge.sh [ruta-al-repo-del-addon]
#
# Por defecto asume que el add-on esta clonado al lado:
#   ../conga-8090-home-assistant
set -euo pipefail

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
ADDON_DIR="${1:-$SRC_DIR/../conga-8090-home-assistant}"
ADDON_BRIDGE_DIR="$ADDON_DIR/conga_8090_bridge"

if [ ! -d "$ADDON_BRIDGE_DIR" ]; then
    echo "ERROR: no encuentro el add-on en: $ADDON_BRIDGE_DIR"
    echo "  Clonalo al lado de este repo:"
    echo "    git clone https://github.com/miguelsg29/conga-8090-home-assistant.git"
    echo "  o pasa la ruta como argumento:  ./sync_bridge.sh /ruta/al/addon"
    exit 1
fi

echo "[sync] copiando el puente al add-on..."
cp "$SRC_DIR/conga_mqtt_bridge.py" "$ADDON_BRIDGE_DIR/conga_mqtt_bridge.py"
echo "  conga_mqtt_bridge.py  ->  conga_8090_bridge/"

# Verificar que el puente quedo identico
if diff -q "$SRC_DIR/conga_mqtt_bridge.py" \
           "$ADDON_BRIDGE_DIR/conga_mqtt_bridge.py" >/dev/null; then
    echo "[sync] OK: el puente del add-on es identico al de este repo."
else
    echo "[sync] AVISO: el puente difiere tras copiar. Revisa manualmente."
    exit 1
fi

VER=$(grep '^version:' "$ADDON_BRIDGE_DIR/config.yaml" | head -1 |
      sed 's/.*"\(.*\)".*/\1/')

cat <<MSG

[sync] Hecho. Antes de subir el add-on, recuerda:
  1) Sube la version en  conga_8090_bridge/config.yaml   (actual: ${VER:-?})
  2) Anade una entrada en conga_8090_bridge/CHANGELOG.md
  3) En el repo del add-on:
       git add -A && git commit -m "Sincroniza puente" && git push
MSG
