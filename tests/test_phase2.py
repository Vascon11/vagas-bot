import json
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from hermes.db import Database
from hermes.evaluate import evaluate_new
from hermes.filters import FilterRules, prefilter
from hermes.models import JobPosting, Status
from hermes.scoring import Score, format_examples
from hermes.search import load_queries, load_rules
from hermes.sources.base import SearchQuery
from hermes.sources.gupy import GupySource

ROOT = Path(__file__).parent.parent
# Resposta real da API da Gupy (29/09/2026), reduzida a três vagas: uma com data
# ISO completa, uma com data sem fuso e uma "banco de talentos".
GUPY = json.loads((Path(__file__).parent / "fixtures/gupy_jobs.json").read_text())
NOW = datetime(2026, 9, 29, 23, 0, tzinfo=UTC)


def gupy_client(pages):
    calls = []

    def handler(request: httpx.Request):
        calls.append(dict(request.url.params))
        return httpx.Response(200, json=pages[len(calls) - 1])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    client.calls = calls
    return client


# --- Gupy ------------------------------------------------------------------


def test_gupy_maps_real_payload():
    src = GupySource(gupy_client([GUPY]), now=lambda: NOW)
    jobs = src.search(SearchQuery(name="t", term="estágio", sites=("gupy",), hours_old=48))
    assert [j.external_id for j in jobs] == ["12574249", "1000018925", "12627900"]
    assert jobs[0].source == "gupy" and jobs[0].posted_at == date(2026, 9, 29)
    assert jobs[0].level == "internship" and jobs[2].level == "talent_pool"
    assert jobs[0].is_remote is False
    assert jobs[0].location and "gupy.io" in jobs[0].url


def test_gupy_applies_time_window_and_term_override():
    client = gupy_client([GUPY])
    late = lambda: datetime(2026, 10, 5, tzinfo=UTC)  # tudo tem mais de 48 h
    q = SearchQuery(name="t", term="estágio desenvolvedor", sites=("gupy",),
                    extra={"gupy_term": "estágio"})
    assert GupySource(client, now=late).search(q) == []
    assert client.calls[0]["jobName"] == "estágio"
    assert client.calls[0]["state"] == "São Paulo"


def test_gupy_is_skipped_when_not_in_sites():
    client = gupy_client([])
    assert GupySource(client).search(SearchQuery(name="t", term="x", sites=("linkedin",))) == []
    assert client.calls == []


def test_gupy_paginates_until_short_page():
    full = {"data": [dict(GUPY["data"][0], id=i) for i in range(100)]}
    client = gupy_client([full, GUPY])
    src = GupySource(client, now=lambda: NOW)
    src.search(SearchQuery(name="t", term="x", sites=("gupy",), results_wanted=500))
    assert [c["offset"] for c in client.calls] == ["0", "100"]


# --- pré-filtro --------------------------------------------------------------

RULES = FilterRules.from_dict(
    {"excluir_no_titulo": ["sênior", "sr", "pleno", "banco de talentos"],
     "excluir_niveis": ["talent_pool"]}
)


@pytest.mark.parametrize(
    "title, level, discarded",
    [
        ("Desenvolvedor Java Pleno", None, True),
        ("Dev Backend SR", None, True),
        ("Engenheiro de Software Senior", None, True),  # sem acento também pega
        ("Estágio de T.I | Holding", "talent_pool", True),
        ("Estágio em Desenvolvimento", "internship", False),
        ("Analista de Sistemas Júnior", "mid-senior level", False),  # rótulo do LinkedIn não conta
        ("Srta. Dev Júnior", None, False),  # "sr" só como palavra inteira
    ],
)
def test_prefilter(title, level, discarded):
    assert (prefilter(title, level, RULES) is not None) is discarded


def test_real_yaml_rules_do_not_drop_mid_senior_label():
    rules = load_rules(ROOT / "searches.yaml")
    assert prefilter("Desenvolvedor FullStack Júnior", "mid-senior level", rules) is None
    # Pleno tem thread própria no Discord; sênior não.
    assert prefilter("Desenvolvedor Java Pleno", None, rules) is None
    assert prefilter("Desenvolvedor Java Sênior", None, rules) is not None


def test_yaml_source_specific_keys_go_to_extra():
    q = {q.name: q for q in load_queries(ROOT / "searches.yaml")}
    assert q["estagio-dev"].extra == {"gupy_term": "estágio"}
    assert q["estagio-ti"].sites == ("linkedin",)
    assert "gupy" in q["junior-dev"].sites


# --- avaliação ----------------------------------------------------------------


def score(n, **kw) -> Score:
    base = dict(score=n, recomendacao="aplicar", nivel_detectado="estagio",
                modelo_trabalho="hibrido", motivos=["stack bate"], alertas=[])
    return Score(**{**base, **kw})


class FakeScorer:
    def __init__(self, scores):
        self.scores = scores  # título -> Score, ou Exception
        self.seen_decisions = None

    def score(self, row, decisions):
        self.seen_decisions = decisions
        r = self.scores[row["title"]]
        if isinstance(r, Exception):
            raise r
        return r


def add(db, title, level=None, n=[0]):
    n[0] += 1
    return db.add_job(JobPosting(source="linkedin", external_id=f"li-{n[0]}", title=title,
                                 company=f"Empresa {n[0]}", url="https://x", level=level))


def test_evaluate_routes_each_job():
    db = Database(":memory:")
    good = add(db, "Estágio Dev")
    low = add(db, "Estágio Marketing")
    pre = add(db, "Dev Pleno")
    boom = add(db, "Estágio Dados")
    scorer = FakeScorer({"Estágio Dev": score(85), "Estágio Marketing": score(30),
                         "Estágio Dados": RuntimeError("timeout")})
    r = evaluate_new(db, scorer, RULES, score_min=60)

    assert r.to_you == [good] and r.low_score == 1 and r.prefiltered == 1
    assert db.get(good)["status"] == Status.AGUARDANDO_VOCE
    assert json.loads(db.get(good)["score_reason"])["motivos"] == ["stack bate"]
    assert db.get(low)["status"] == Status.DESCARTADA
    assert "pré-filtro" in db.events(pre)[-1]["detail"]
    # erro de rede não descarta: a vaga fica "nova" para a próxima rodada
    assert db.get(boom)["status"] == Status.NOVA and len(r.errors) == 1


def test_evaluate_passes_recent_decisions_as_examples():
    db = Database(":memory:")
    old = add(db, "Estágio Suporte")
    db.set_decision(old, "descartou")
    add(db, "Estágio Dev")
    scorer = FakeScorer({"Estágio Dev": score(70)})
    evaluate_new(db, scorer, RULES, score_min=60)
    assert [d["title"] for d in scorer.seen_decisions] == ["Estágio Suporte"]
    assert "DESCARTOU: Estágio Suporte" in format_examples(scorer.seen_decisions)


def test_phase1_database_is_migrated(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE jobs (id INTEGER PRIMARY KEY, source TEXT, external_id TEXT, "
        "dedup_key TEXT, title TEXT, company TEXT, url TEXT, location TEXT, description TEXT, "
        "posted_at TEXT, level TEXT, is_remote INTEGER, score INTEGER, score_reason TEXT, "
        "status TEXT, found_at TEXT, status_at TEXT);"
    )
    conn.close()
    db = Database(path)
    assert db.recent_decisions() == []


def test_fatal_error_stops_the_round():
    from hermes.scoring import FatalScoringError

    db = Database(":memory:")
    a, b = add(db, "Estágio A"), add(db, "Estágio B")
    calls = []

    class NoCredit:
        def score(self, row, decisions):
            calls.append(row["id"])
            raise FatalScoringError("You have no credits remaining")

    r = evaluate_new(db, NoCredit(), RULES, score_min=60)
    assert calls == [a]  # não tenta a segunda
    assert r.errors == ["rodada interrompida: You have no credits remaining"]
    assert db.get(b)["status"] == Status.NOVA


def test_gemini_falls_back_on_503_but_not_on_other_errors():
    from google.genai import errors

    from hermes.scoring import GeminiScorer

    calls = []

    class Models:
        def __init__(self, fail_code):
            self.fail_code = fail_code

        def generate_content(self, model, contents, config):
            calls.append(model)
            if model == "principal":
                cls = errors.ServerError if self.fail_code >= 500 else errors.ClientError
                raise cls(self.fail_code, {"error": {"code": self.fail_code, "message": "x"}})
            return type("R", (), {"parsed": score(77)})()

    class Client:
        def __init__(self, code):
            self.models = Models(code)

    row = {"id": 1, "title": "t", "company": "c", "location": None, "source": "gupy",
           "description": "d"}
    assert GeminiScorer(Client(503), "principal", "p", "reserva").score(row, []).score == 77
    assert calls == ["principal", "reserva"]

    calls.clear()
    with pytest.raises(errors.ClientError):
        GeminiScorer(Client(400), "principal", "p", "reserva").score(row, [])
    assert calls == ["principal"]
