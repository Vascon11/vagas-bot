# Hermes

Assistente pessoal de busca e acompanhamento de vagas. Coleta vagas, dá nota contra o
perfil, pede aprovação pelo Telegram e acompanha a candidatura pelo e-mail até a
recusa, a contratação ou 90 dias sem resposta. **Nunca se candidata sozinho.**

Estado atual: **fase 1** — coleta do LinkedIn (via [JobSpy](https://github.com/speedyapply/JobSpy),
sem login), banco SQLite com máquina de estados e deduplicação.

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m hermes buscar --dry-run          # só imprime
.venv/bin/python -m hermes buscar --only estagio-dev # grava em data/hermes.db
.venv/bin/pytest
```

As buscas ficam em `searches.yaml`. Mantenha o volume baixo: é o que protege o IP
de bloqueio pelo LinkedIn.

Próximas fases: Gupy + pré-filtro + nota com a OpenAI · bot do Telegram ·
leitura do Outlook via Microsoft Graph · deploy no ZimaOS.
