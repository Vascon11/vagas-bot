# Hermes

Assistente pessoal de busca e acompanhamento de vagas. Coleta vagas, dá nota contra o
perfil, pede aprovação pelo Telegram e acompanha a candidatura pelo e-mail até a
recusa, a contratação ou 90 dias sem resposta. **Nunca se candidata sozinho.**

Estado atual: **todas as fases do MVP**
- Coleta: LinkedIn via [JobSpy](https://github.com/speedyapply/JobSpy) (sem login) e a API
  pública da Gupy.
- Banco SQLite com máquina de estados e deduplicação entre fontes.
- Pré-filtro por título/tipo (sem custo) e nota de 0 a 100 com a API da OpenAI, calibrada
  pelas últimas decisões do usuário (Gemini por padrão; OpenAI como alternativa).
- Bot do Telegram: cards com ✅ Vou aplicar / 📨 Apliquei / ❌ Descartar, comandos
  `/pendentes`, `/aplicadas`, `/buscar`, `/resumo`, busca a cada 2 h, resumo às 8h e
  varredura dos 90 dias sem resposta.
- E-mail: o Outlook redireciona os e-mails de recrutamento para um Gmail, lido por IMAP
  (só leitura, e só as mensagens endereçadas ao Outlook). O LLM classifica (confirmação,
  teste, entrevista, recusa…), liga à candidatura e atualiza o estado; se não souber de
  qual vaga é, pergunta no Telegram com botões.
- Deploy: `./deploy/deploy.sh` publica no servidor como o container `vagas-bot`.

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m hermes buscar --dry-run          # só imprime
.venv/bin/python -m hermes buscar --only estagio-dev # grava em data/hermes.db
.venv/bin/python -m hermes avaliar --limit 5         # pré-filtro + nota (precisa de OPENAI_API_KEY)
.venv/bin/python -m hermes rodar                     # buscar + avaliar
.venv/bin/python -m hermes listar                    # vagas esperando você, pela nota
.venv/bin/python -m hermes bot                       # modo de produção: bot + agendamento
.venv/bin/pytest
```

Configuração: `.env` (copie de `.env.example`), `profile.md` (copie de `profile.example.md`;
fica fora do git) e `searches.yaml`, com as buscas e o pré-filtro. Mantenha o volume baixo: é o que protege o IP
de bloqueio pelo LinkedIn.

