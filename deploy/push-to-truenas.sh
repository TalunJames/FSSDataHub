#!/bin/sh
# Copy this project from your Mac to TrueNAS and build there.
#
#   NAS=truenas.local sh deploy/push-to-truenas.sh
#
# TrueNAS must have SSH enabled (System → Services → SSH) and you must be
# able to log in as a user who can write the datasets and run docker.
#
# sql/ is a nested dataset for tax.db — rsync never touches it.

set -eu

NAS="${NAS:-truenas.local}"
USER="${USER_NAS:-root}"
APP_DIR="${APP_DIR:-/mnt/Seawolf/FogSignal/taxdata}"
DATA_DIR="${DATA_DIR:-/mnt/Seawolf/FogSignal/taxdata/sql}"
ROOT="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"

echo "Syncing ${ROOT} → ${USER}@${NAS}:${APP_DIR}"
ssh "${USER}@${NAS}" "mkdir -p '${APP_DIR}' '${DATA_DIR}'"

rsync -az --delete \
  --exclude '.git' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude '.compose.truenas.generated.yml' \
  --exclude 'cache/' \
  --exclude 'out/' \
  --exclude 'archive/' \
  --exclude 'sql/' \
  --exclude 'sql' \
  --exclude '*.db' \
  --exclude '*.db-wal' \
  --exclude '*.db-shm' \
  "${ROOT}/" "${USER}@${NAS}:${APP_DIR}/"

echo "Building and starting on the NAS…"
ssh "${USER}@${NAS}" "APP_DIR='${APP_DIR}' DATA_DIR='${DATA_DIR}' sh '${APP_DIR}/deploy/truenas-setup.sh'"
