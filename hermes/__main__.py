import argparse
import logging
import sys

from hermes.config import Settings
from hermes.db import Database
from hermes.search import load_queries, run_searches
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
        [JobSpySource()],
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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="hermes")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("buscar", help="roda as buscas do searches.yaml")
    p.add_argument("--dry-run", action="store_true", help="só imprime, não grava no banco")
    p.add_argument("--only", metavar="NOME", help="roda só a busca com esse nome")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return {"buscar": cmd_buscar}[args.cmd](args, Settings())


if __name__ == "__main__":
    sys.exit(main())
