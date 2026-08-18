#!/bin/sh
# Build and start the collector on TrueNAS SCALE.
# Run this ON the NAS, from the copied repo:
#
#   sh deploy/truenas-setup.sh
#
# SQLite, archive, and intake uploads live on the sql dataset.

set -eu

APP_DIR="${APP_DIR:-/mnt/Seawolf/FogSignal/taxdata}"
DATA_DIR="${DATA_DIR:-/mnt/Seawolf/FogSignal/taxdata/sql}"
IMAGE="${IMAGE:-taxdb-collector:local}"
HOST_PORT="${HOST_PORT:-8080}"

if [ ! -f "${APP_DIR}/Dockerfile" ]; then
  echo "No Dockerfile at ${APP_DIR}."
  echo "Copy this project onto the NAS first (see TRUENAS.md), then re-run."
  exit 1
fi

if [ ! -d "${DATA_DIR}" ]; then
  echo "Create a dataset at ${DATA_DIR} in the TrueNAS UI, then re-run."
  exit 1
fi

mkdir -p "${DATA_DIR}/archive" "${DATA_DIR}/cache" "${DATA_DIR}/out" "${DATA_DIR}/intake"

echo "Building ${IMAGE} on this NAS (do not build on a Mac — architecture will not match)…"
docker build -t "${IMAGE}" "${APP_DIR}"

COMPOSE="${APP_DIR}/.compose.truenas.generated.yml"
sed -e "s|/mnt/Seawolf/FogSignal/taxdata/sql|${DATA_DIR}|g" \
    -e "s|\"8080:8080\"|\"${HOST_PORT}:8080\"|g" \
    "${APP_DIR}/compose.truenas.yml" > "${COMPOSE}"

echo "Starting collector with data on ${DATA_DIR}…"
docker compose -f "${COMPOSE}" up -d --no-build

echo
echo "Collector is running and will start again after a reboot."
echo "Open  http://$(hostname -s 2>/dev/null || echo NAS-IP):${HOST_PORT}"
echo "Database file: ${DATA_DIR}/tax.db"
echo
echo "Optional — copy a Mac database onto the NAS (stop the app first if it is running):"
echo "  scp tax.db root@NAS:${DATA_DIR}/tax.db"
