import argparse
import json
import logging
import sys

from hermes.config import Settings
from hermes.db import Database
from hermes.evaluate import evaluate_new
from hermes.models import Status
from hermes.search import load_queries, load_rules, run_searches
from hermes.sources.gupy import GupySource
from hermes.sources.jobspy_source import JobSpySource


def cmd_buscar(args, settings: Settings) -> int:
    queries = load_queries(settings.searches_file)
    if args.only:
        queries = [q for q in queries if q.name == args.only]
        if not queries:
            print(f"Nenhuma busca chamada {args.only!r} em {settings.searches_file}")
            return 1
    db = None if args.dry_run else Database(settings.db_path)
    result = run_searches(
        queries,
        [JobSpySource(), GupySource()],
        db,
        pause=(settings.pause_min, settings.pause_max),
    )
    for p in result.found:
        when = p.posted_at.isoformat() if p.posted_at else "   ?      "
        print(f"[{p.source:8}] {when}  {p.title} — {p.company}\n{'':22}{p.url}")
    print(f"\n{len(result.found)} vagas encontradas", end="")
    print(" (dry-run, nada gravado)" if args.dry_run else f", {len(result.new_ids)} novas no banco")
    for err in result.errors:
        print(f"ERRO: {err}", file=sys.stderr)
    return 1 if result.errors and not result.found else 0


def cmd_avaliar(args, settings: Settings) -> int:
    if not settings.llm_api_key:
        key = {"openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY"}[settings.llm_provider]
        print(f"{key} não está no .env (HERMES_LLM_PROVIDER={settings.llm_provider})",
              file=sys.stderr)
        return 1
    if not settings.profile_file.exists():
        print(f"{settings.profile_file} não existe — copie de profile.example.md", file=sys.stderr)
        return 1
    from hermes.scoring import make_scorer

    scorer = make_scorer(
        settings.llm_provider,
        settings.llm_api_key,
        settings.model,
        settings.profile_file.read_text(encoding="utf-8"),
        settings.llm_fallback_model or None,
    )
    db = Database(settings.db_path)
    r = evaluate_new(db, scorer, load_rules(settings.searches_file), settings.score_min, args.limit)
    print(
        f"{len(r.to_you)} para você · {r.low_score} com nota baixa · "
        f"{r.prefiltered} no pré-filtro · {len(r.errors)} erros"
    )
    for job_id in r.to_you:
        print_job(db.get(job_id))
    for err in r.errors:
        print(f"ERRO: {err}", file=sys.stderr)
    return 1 if r.errors and not (r.to_you or r.low_score) else 0


def cmd_rodar(args, settings: Settings) -> int:
    args.dry_run, args.only = False, None
    cmd_buscar(args, settings)
    return cmd_avaliar(args, settings)


def print_job(row) -> None:
    print(f"\n#{row['id']}  [{row['score'] if row['score'] is not None else '–':>3}]  "
          f"{row['title']} — {row['company']}  ({row['location'] or '?'})")
    if row["score_reason"]:
        s = json.loads(row["score_reason"])
        print(f"      {s['recomendacao']} · {s['nivel_detectado']} · {s['modelo_trabalho']}")
        for m in s["motivos"]:
            print(f"      + {m}")
        for a in s["alertas"]:
            print(f"      ! {a}")
    print(f"      {row['url']}")


def cmd_listar(args, settings: Settings) -> int:
    db = Database(settings.db_path)
    rows = db.by_status(Status(args.status))
    for row in sorted(rows, key=lambda r: -(r["score"] or 0)):
        print_job(row)
    print(f"\n{len(rows)} vagas em {args.status}")
    return 0


def cmd_fila(args, settings: Settings) -> int:
    """Vagas que o Gabriel aprovou e ainda não aplicou — a fila de candidaturas."""
    db = Database(settings.db_path)
    rows = db.by_status(Status.APROVADA)
    if args.todas:
        rows += db.by_status(Status.AGUARDANDO_VOCE)
    if args.json:
        keys = ("id", "status", "title", "company", "location", "url", "score", "source")
        out = [{k: r[k] for k in keys} | {"avaliacao": json.loads(r["score_reason"] or "null"),
                                          "descricao": r["description"]} for r in rows]
        print(json.dumps(out, ensure_ascii=False, indent=1))
    else:
        for row in rows:
            print_job(row)
        print(f"\n{len(rows)} vagas na fila")
    return 0


def cmd_marcar(args, settings: Settings) -> int:
    from hermes.models import InvalidTransition

    db = Database(settings.db_path)
    new = Status(args.status)
    try:
        db.transition(args.id, new, args.nota or "pela linha de comando")
    except KeyError:
        print(f"vaga #{args.id} não existe", file=sys.stderr)
        return 1
    except InvalidTransition as exc:
        print(f"não mudei a vaga #{args.id}: {exc}", file=sys.stderr)
        return 1
    if new in (Status.APROVADA, Status.APLICADA):
        db.set_decision(args.id, "aprovou")
    elif new == Status.DESCARTADA:
        db.set_decision(args.id, "descartou")
    row = db.get(args.id)
    print(f"#{row['id']} {row['title']} — {row['company']}: {row['status']}")
    return 0


def cmd_bot(args, settings: Settings) -> int:
    from hermes.bot import run

    run(settings)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="hermes")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("buscar", help="roda as buscas do searches.yaml")
    p.add_argument("--dry-run", action="store_true", help="só imprime, não grava no banco")
    p.add_argument("--only", metavar="NOME", help="roda só a busca com esse nome")

    p = sub.add_parser("avaliar", help="pré-filtro + nota do LLM nas vagas novas")
    p.add_argument("--limit", type=int, help="avalia no máximo N vagas (bom para testar)")

    p = sub.add_parser("rodar", help="buscar e avaliar em sequência")
    p.add_argument("--limit", type=int)

    p = sub.add_parser("listar", help="lista vagas de um estado, pela nota")
    p.add_argument("status", nargs="?", default=Status.AGUARDANDO_VOCE,
                   choices=[s.value for s in Status])

    p = sub.add_parser("fila", help="vagas aprovadas esperando candidatura")
    p.add_argument("--json", action="store_true", help="saída em JSON, com a descrição")
    p.add_argument("--todas", action="store_true", help="inclui as que ainda esperam decisão")

    p = sub.add_parser("marcar", help="muda o estado de uma vaga (ex.: depois de aplicar)")
    p.add_argument("id", type=int)
    p.add_argument("status", choices=[s.value for s in Status])
    p.add_argument("--nota", help="texto que fica no histórico da vaga")

    sub.add_parser("bot", help="bot do Telegram + buscas agendadas (o modo de produção)")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # O httpx registra a URL de cada chamada em INFO, e na API do Telegram o token
    # vai na URL. Fora do -v isso já não aparece; com -v, o token acabaria no log.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    commands = {"buscar": cmd_buscar, "avaliar": cmd_avaliar, "rodar": cmd_rodar,
                "listar": cmd_listar, "fila": cmd_fila, "marcar": cmd_marcar, "bot": cmd_bot}
    return commands[args.cmd](args, Settings())


if __name__ == "__main__":
    sys.exit(main())
