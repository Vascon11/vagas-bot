#!/usr/bin/env bash
# Publica o bot de vagas no ZimaOS.
#
#   ./deploy/deploy.sh     testes + envio + build da imagem + (re)start
#
# Só opera dentro de /DATA/AppData/vagas-bot e do projeto compose `vagas-bot`.
# Não mexe no container `hermes` (Hermes Agent) nem em nada mais da máquina.
set -euo pipefail

HOST="${VAGAS_HOST:-gabriel@100.121.7.119}"
REMOTE_DIR="/DATA/AppData/vagas-bot"

cd "$(dirname "$0")/.."

# Dois processos ao mesmo tempo postam cada vaga duas vezes no Discord (e, no modo
# bot, brigam pelo Telegram com 409 Conflict).
if pgrep -f "python -m hermes .*(bot|canal)" >/dev/null; then
  echo "✗ o bot está rodando nesta máquina — pare-o antes"
  exit 1
fi

for f in .env profile.md searches.yaml; do
  [[ -s "$f" ]] || { echo "✗ $f ausente ou vazio"; exit 1; }
done

echo "▸ Testes"
.venv/bin/pytest -q -p no:warnings

echo "▸ Testando acesso ao servidor"
ssh -o ConnectTimeout=10 -o BatchMode=yes "$HOST" true \
  || { echo "✗ sem acesso a $HOST (o ZimaOS está online? tailscale status)"; exit 1; }

echo "▸ Enviando"
ssh "$HOST" "mkdir -p $REMOTE_DIR/app $REMOTE_DIR/config $REMOTE_DIR/data"
# --delete só dentro de app/: código que saiu do repositório sai da imagem também.
rsync -az --delete --exclude '__pycache__' \
  Dockerfile .dockerignore pyproject.toml hermes "$HOST:$REMOTE_DIR/app/"
rsync -az searches.yaml profile.md "$HOST:$REMOTE_DIR/config/"
rsync -az deploy/compose.yml "$HOST:$REMOTE_DIR/compose.yml"
# Segredos: só o dono lê. O daemon do Docker (root) lê para o env_file.
rsync -az --chmod=F600 .env "$HOST:$REMOTE_DIR/.env"

# O host não tem o plugin `docker compose`: ele vem de um container efêmero,
# mesmo padrão do resume e do ImovAI nesta máquina.
compose() {
  ssh "$HOST" "docker run --rm -e HOME=/tmp \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v $REMOTE_DIR:$REMOTE_DIR -w $REMOTE_DIR \
    docker:28-cli docker compose $*"
}

echo "▸ Build e start"
# Nunca `down` nem `--remove-orphans`: há outros projetos compose nesta engine.
compose build --quiet vagas-bot
compose up -d vagas-bot

echo "▸ Esperando o healthcheck"
for _ in $(seq 1 24); do
  status=$(ssh "$HOST" "docker inspect -f '{{.State.Health.Status}}' vagas-bot" 2>/dev/null || echo "?")
  [[ "$status" == "healthy" ]] && break
  [[ "$status" == "unhealthy" ]] && break
  sleep 10
done
echo "  vagas-bot: $status"
ssh "$HOST" "docker logs --tail 15 vagas-bot 2>&1"
[[ "$status" == "healthy" ]] && echo "✓ Publicado" || { echo "✗ não ficou healthy"; exit 1; }
