#!/usr/bin/env bash
# Roda um comando do bot dentro do container no servidor, contra o banco de produção.
#
#   ./deploy/remote.sh fila --json
#   ./deploy/remote.sh marcar 42 aplicada --nota "aplicado pelo Claude no Chrome"
set -euo pipefail
HOST="${VAGAS_HOST:-gabriel@100.121.7.119}"
args=$(printf '%q ' "$@")
exec ssh "$HOST" "docker exec vagas-bot python -m hermes $args" 2> >(grep -v 'config.json' >&2)
