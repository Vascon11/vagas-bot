# vagas-bot

Coleta vagas de estágio e júnior em São Paulo (LinkedIn, Gupy e os programas públicos
da Companhia de Estágios), descarta as que não
servem e posta as que sobram num canal do Discord. **Nunca se candidata sozinho.**

## Modo canal (o que roda em produção)

`python -m hermes canal`: a cada 2 h busca, aplica o pré-filtro e posta no Discord por
webhook, cada vaga na thread do seu nível. **Não guarda vaga em disco**: sem banco, sem
Telegram e, por padrão, sem LLM. Pra não repetir
vaga, lembra na memória do processo o que já postou, por 7 dias
(`HERMES_REMEMBER_DAYS`). Num restart a memória zera; como a busca só olha as últimas
3 h (`hours_old` no `searches.yaml`), o que pode repetir é pouco.

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
cp .env.example .env                                # basta o DISCORD_WEBHOOK_URL
.venv/bin/python -m hermes buscar --dry-run          # só imprime o que a busca acha
.venv/bin/python -m hermes canal                     # o loop que posta no Discord
.venv/bin/pytest
```

## Onde mexer no filtro das vagas

Sem LLM, quem decide o que vira card é só o pré-filtro:

- `searches.yaml`: os termos buscados em cada fonte e a lista `excluir_no_titulo`.
  O primeiro suspeito das vagas fora da área é o `gupy_term: "estágio"` da busca
  `estagio-dev`: na Gupy ele traz estágio de qualquer área (engenharia civil,
  secretariado…).
- `hermes/filters.py`: a função `prefilter(title, level, rules)`, que devolve o motivo do
  descarte ou `None`. É o ponto pra trocar por uma análise melhor (palavras-chave de área,
  peso por termo, leitura da descrição etc.). A vaga inteira (com `description`) está no
  `JobPosting`, em `hermes/models.py`.
- `hermes/canal.py`: `to_post()` é onde o pré-filtro é aplicado no modo canal.

## Threads por nível

Cada vaga vai para uma thread do canal conforme o nível: estágio, júnior, pleno,
trainee… Os IDs ficam em `discord.threads` no `searches.yaml`; vaga sem nível, ou de um
nível sem thread, vai para `outros`. O rodapé do card mostra o nível e de onde ele veio
(`gupy · Pleno pela descrição`), pra dar pra ver quando a classificação erra.

- `hermes/filters.py`: `explain_level()` decide o nível, do sinal mais forte para o
  mais fraco: o título (com sinônimos e Analista I/II/III), o tipo da vaga na fonte, a
  descrição (frases como "bolsa-auxílio", "nível pleno", anos de experiência pedidos,
  responsabilidades de sênior), cargo de entrada (assistente, auxiliar) e o
  "entry level" do LinkedIn. Os limites de anos foram calibrados em ~750 vagas reais.
- LLM opcional: com `HERMES_LEVEL_LLM_PER_DAY` > 0 e a chave do provedor no `.env`, as
  vagas que as regras não resolvem vão para o LLM, numa chamada por ciclo (até 15
  vagas, só o trecho dos requisitos: ~240 tokens por vaga) e com esse teto de vagas por
  dia. Erro, falta de chave ou teto batido mandam a vaga para `outros`.
- `tests/test_phase2.py` e `tests/test_canal.py`: os testes. Rode `.venv/bin/pytest`
  antes de mandar PR.

## Modo bot (antigo, com banco)

`python -m hermes bot` é a versão anterior: guarda as vagas em SQLite, dá nota de 0 a 100
com LLM (Gemini ou OpenAI) contra um `profile.md`, manda cards com botões no Telegram,
acompanha a candidatura pelo e-mail (IMAP) e marca "sem resposta" depois de 90 dias. Os
comandos `buscar`, `avaliar`, `rodar`, `listar`, `fila` e `marcar` são desse modo e usam
`data/hermes.db`. O código continua aqui, mas não é o que está em produção.

Deploy no servidor: `./deploy/deploy.sh` (container `vagas-bot`).
