FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 \
    TZ=America/Sao_Paulo
WORKDIR /app

# Dependências antes do código: mudar só o código não refaz o pip install.
COPY pyproject.toml ./
RUN mkdir hermes && touch hermes/__init__.py \
 && pip install . && pip uninstall -y hermes && rm -rf hermes

COPY hermes ./hermes
RUN pip install --no-deps .

# Mesmo uid do usuário gabriel no ZimaOS: os arquivos em data/ continuam editáveis
# por ele fora do container.
RUN useradd --uid 999 --gid 1000 --no-create-home --home-dir /app vagas 2>/dev/null \
 || (groupadd --gid 1000 samba && useradd --uid 999 --gid 1000 --no-create-home --home-dir /app vagas)
USER 999:1000

# Saudável = o bot gravou o heartbeat nos últimos 5 minutos.
HEALTHCHECK --interval=60s --timeout=10s --start-period=90s --retries=3 \
  CMD python -c "import os,sys,time; sys.exit(time.time()-os.path.getmtime('/app/data/heartbeat')>300)"

CMD ["python", "-m", "hermes", "canal"]
