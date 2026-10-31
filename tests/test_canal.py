import asyncio
import json

import httpx

from hermes import canal
from hermes.canal import LevelLLM, Seen, as_row, cycle, thread_for, to_post
from hermes.config import Settings
from hermes.discord import card_embed, post_card
from hermes.filters import FilterRules, classify_level, explain_level, prefilter
from hermes.models import JobPosting
from hermes.scoring import (EXCERPT, FatalScoringError, LevelBatch, LevelGuess, Scorer,
                            requirements_excerpt)
from hermes.search import load_threads

URL = "https://discord.com/api/webhooks/1/segredo"


def job(title, company="Acme", url="https://acme.gupy.io/job/1"):
    return JobPosting(source="gupy", external_id=title, title=title, company=company, url=url)


class FakeSource:
    def __init__(self, postings):
        self.postings = postings

    def search(self, query):
        return self.postings


def settings(tmp_path):
    searches = tmp_path / "searches.yaml"
    searches.write_text(
        "searches:\n  - {name: s, term: estágio, sites: [gupy]}\n"
        "filtros:\n  excluir_no_titulo: [pleno]\n", encoding="utf-8")
    return Settings(searches_file=searches, data_dir=tmp_path, pause_min=0, pause_max=0,
                    discord_webhook_url=URL, _env_file=None)


def test_seen_forgets_after_ttl():
    clock = [0.0]
    seen = Seen(days=1, now=lambda: clock[0])
    seen.add("a")
    clock[0] = 86400 - 1
    seen.prune()
    assert "a" in seen
    clock[0] = 86400 + 1
    seen.prune()
    assert "a" not in seen and len(seen) == 0


def test_to_post_drops_prefiltered_and_already_posted():
    seen = Seen(days=7)
    seen.add(job("Estágio Dados").dedup_key)
    found = [job("Estágio Dados"), job("Dev Pleno"), job("Estágio Dev")]
    assert [p.title for p in to_post(found, FilterRules(exclude_title=("pleno",)), seen)] == ["Estágio Dev"]


def test_cycle_posts_once_and_retries_failures(tmp_path, monkeypatch):
    monkeypatch.setattr(canal, "POST_GAP", 0)
    s, seen, posted = settings(tmp_path), Seen(days=7), []
    source = FakeSource([job("Estágio Dev"), job("Dev Pleno"), job("Estágio QA")])
    fail = {"Estágio QA"}

    async def post(url, row, thread_id=None):
        posted.append(row["title"])
        return row["title"] not in fail

    assert asyncio.run(cycle(s, seen, post=post, sources=[source])) == 1
    fail.clear()
    # Estágio Dev já foi; Estágio QA falhou antes e vai de novo.
    assert asyncio.run(cycle(s, seen, post=post, sources=[source])) == 1
    assert posted == ["Estágio Dev", "Estágio QA", "Estágio QA"]
    assert not (tmp_path / "hermes.db").exists()


def test_400_retries_without_clickable_url():
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(400 if len(sent) == 1 else 204)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    row = as_row(job("Estágio", url="https://novos_talentos.gupy.io/job/1"),
                 "estagio", "pelo título")
    assert asyncio.run(post_card(URL, row, client))
    retry = sent[1]["embeds"][0]
    assert "url" not in retry and "https://novos_talentos.gupy.io/job/1" in retry["description"]
    assert retry["footer"]["text"] == "gupy · Estágio pelo título"


def test_classify_level_by_title_then_source():
    assert classify_level("Estágio em Desenvolvimento", None) == "estagio"
    assert classify_level("Estagiária de Dados", None) == "estagio"
    assert classify_level("Desenvolvedor Jr. Python", None) == "junior"
    assert classify_level("Dev Pleno", "internship") == "pleno"  # o título manda
    assert classify_level("Programa Trainee 2027", None) == "trainee"
    assert classify_level("Analista de Sistemas", "vacancy_type_effective") is None
    assert classify_level("Analista de Sistemas", "internship") == "estagio"
    assert classify_level("Analista de Sistemas", "mid-senior level") is None


def test_classify_level_title_synonyms_and_roman_numerals():
    assert classify_level("Analista de Marketing III", None) == "senior"
    assert classify_level("Analista de Dados II", None) == "pleno"
    assert classify_level("Software Engineer I", None) == "junior"
    assert classify_level("Analista de Suporte PL I", None) == "pleno"  # pleno I, não júnior
    assert classify_level("Analista Contábil PL", None) == "pleno"
    assert classify_level("Desenvolvedor PL/SQL", None) is None
    assert classify_level("Analista de T.I.", None) is None
    assert classify_level("Engenheiro de I.A.", None) is None


def test_classify_level_reads_the_description_when_the_title_does_not_say():
    def lvl(desc, title="Analista de Sistemas", level=None):
        return classify_level(title, level, desc)

    assert lvl("Buscamos alguém com nível sênior para liderar") == "senior"
    assert lvl("Procuramos uma pessoa desenvolvedora pleno") == "pleno"
    assert lvl("Requisitos: 1 ano de experiência com SQL") == "junior"
    assert lvl("Experiência mínima de 2 a 4 anos em suporte") == "pleno"
    assert lvl("Mais de 5 anos de experiência com Java") == "senior"
    assert lvl("3+ years of experience with Python") == "pleno"
    assert lvl("Ideal para recém-formados") == "junior"
    assert lvl("Bolsa-auxílio de R$ 1.800") == "estagio"
    assert lvl("Programa Jovem Aprendiz com bolsa-auxílio") == "aprendiz"
    # a idade da empresa não é requisito; "cursando" aparece em vaga de qualquer nível
    assert lvl("Somos uma empresa com 30 anos de experiência no mercado") is None
    assert lvl("Ensino superior completo ou cursando") is None
    # a Gupy cola os blocos e manda &nbsp;
    assert lvl("Sobre nósRequisitosExperiência de&nbsp;2 anos") == "pleno"


def test_classify_level_order_of_evidence():
    # título > nível confiável da fonte > descrição > cargo de entrada > entry level
    assert classify_level("Dev Júnior", None, "5 anos de experiência") == "junior"
    assert classify_level("Analista", "internship", "5 anos de experiência") == "estagio"
    assert classify_level("Assistente de TI", None, "3 anos de experiência") == "pleno"
    assert classify_level("Assistente de TI", None, "Venha crescer conosco") == "junior"
    assert classify_level("Analista de Dados", "entry level", None) == "junior"
    assert classify_level("Analista de Dados", "entry level", "4 anos de experiência") == "senior"


def test_thread_for_falls_back_to_outros_then_webhook():
    threads = {"estagio": "11", "outros": "99"}
    assert thread_for("estagio", threads) == "11"
    assert thread_for("junior", threads) == "99"  # júnior sem thread
    assert thread_for(None, threads) == "99"  # nível desconhecido
    assert thread_for(None, {"estagio": "11"}) is None


def test_explain_level_says_where_the_level_came_from():
    assert explain_level("Estágio Dev", None) == ("estagio", "pelo título")
    assert explain_level("Analista", "internship") == ("estagio", "pela fonte")
    assert explain_level("Analista", None, "3 anos de experiência") == (
        "pleno", "pela descrição")
    assert explain_level("Assistente de TI", None) == ("junior", "pelo cargo")
    assert explain_level("Analista", "entry level") == ("junior", "pela fonte")
    assert explain_level("Analista", None, "Venha crescer conosco") == (None, None)


def test_senior_responsibilities_in_the_description():
    assert classify_level("Analista de Dados", None, "Ser referência técnica do time") == "senior"
    assert classify_level("Analista", None, "Vai liderar a squad de dados") == "senior"
    # anos e nível citado vêm antes
    assert classify_level("Analista", None, "Liderar projetos. 1 ano de experiência") == "junior"


def test_prefilter_drops_gupy_test_postings():
    rules = FilterRules()
    assert prefilter("[Modelo] - Pessoa Desenvolvedora Fullstack", None, rules) == "vaga de teste"
    assert prefilter("[TESTE CF] Regressivo V0 - CR Tecnologia", None, rules) == "vaga de teste"
    assert prefilter("Dev Fullstack - TESTE CONVOCAÇÃO", None, rules) == "vaga de teste"
    assert prefilter("Analista de Testes / QA", None, rules) is None
    assert prefilter("Estágio em Modelagem de Dados", None, rules) is None


def test_as_row_puts_level_and_reason_in_the_card_footer():
    assert card_embed(as_row(job("Dev"), "pleno", "pela descrição"))["footer"]["text"] == \
        "gupy · Pleno pela descrição"
    assert card_embed(as_row(job("Dev")))["footer"]["text"] == "gupy · nível não identificado"


def test_load_threads_ignores_empty_ids(tmp_path):
    f = tmp_path / "searches.yaml"
    f.write_text("discord:\n  threads:\n    estagio: 1558221390463631452\n    junior: 0\n"
                 "    pleno: ''\n", encoding="utf-8")
    assert load_threads(f) == {"estagio": "1558221390463631452"}
    f.write_text("searches: []\n", encoding="utf-8")
    assert load_threads(f) == {}


def test_cycle_posts_each_level_to_its_thread(tmp_path, monkeypatch):
    monkeypatch.setattr(canal, "POST_GAP", 0)
    s = settings(tmp_path)
    s.searches_file.write_text(
        s.searches_file.read_text(encoding="utf-8")
        + "discord:\n  threads: {estagio: 11, junior: 22}\n", encoding="utf-8")
    source = FakeSource([job("Estágio Dev"), job("Dev Júnior"), job("Analista de TI")])
    sent = {}

    async def post(url, row, thread_id=None):
        sent[row["title"]] = thread_id
        return True

    asyncio.run(cycle(s, Seen(days=7), post=post, sources=[source]))
    assert sent == {"Estágio Dev": "11", "Dev Júnior": "22", "Analista de TI": None}


def test_post_card_sends_thread_id_and_replaces_the_one_in_the_url():
    urls = []

    def handler(request):
        urls.append(request.url)
        return httpx.Response(204)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    row = as_row(job("Estágio"))
    assert asyncio.run(post_card(URL, row, client, thread_id="11"))
    assert asyncio.run(post_card(URL + "?thread_id=5", row, client, thread_id="11"))
    assert asyncio.run(post_card(URL + "?thread_id=5", row, client))
    assert [u.params.get("thread_id") for u in urls] == ["11", "11", "5"]
    assert all(u.path == "/api/webhooks/1/segredo" for u in urls)


# --- LLM do nível ---------------------------------------------------------------


class FakeLevels:
    """Faz o papel do Scorer: responde `answers` (título -> nível) ou levanta `error`."""

    def __init__(self, answers=None, error=None):
        self.answers, self.error, self.calls = answers or {}, error, []

    def levels(self, jobs):
        self.calls.append([title for title, _ in jobs])
        if self.error:
            raise self.error
        return [self.answers.get(title) for title, _ in jobs]


def llm(fake, per_day=10, day=None):
    day = day if day is not None else [1]
    return LevelLLM(fake, per_day, lambda: day[0])


def test_requirements_excerpt_starts_at_the_requirements_and_is_short():
    desc = "Sobre nós: líderes há 30 anos.MissãoRequisitos&nbsp;3 anos de Java" + " x" * 2000
    out = requirements_excerpt(desc)
    assert out.startswith("Requisitos 3 anos de Java") and len(out) <= EXCERPT
    assert requirements_excerpt("Venha trabalhar conosco") == "Venha trabalhar conosco"
    assert requirements_excerpt(None) == "(sem descrição)"


def test_scorer_levels_numbers_the_batch_and_maps_indefinido_to_none():
    seen = {}

    class Fake(Scorer):
        def _generate(self, instructions, text, schema):
            seen["text"], seen["schema"] = text, schema
            return LevelBatch(vagas=[LevelGuess(vaga=1, nivel="pleno"),
                                     LevelGuess(vaga=2, nivel="indefinido")])  # a 3 não veio

    out = Fake(None, "m", "").levels([("Analista A", "Requisitos: 3 anos"), ("B", None), ("C", "")])
    assert out == ["pleno", None, None]
    assert "[1] Analista A\nRequisitos: 3 anos" in seen["text"] and "[3] C" in seen["text"]
    assert seen["schema"] is LevelBatch


def test_level_llm_only_pays_once_per_job_and_respects_the_daily_limit():
    fake = FakeLevels({"A": "pleno", "B": None, "C": "junior"})
    day = [1]
    guess = llm(fake, per_day=2, day=day)
    abc = [job("A"), job("B"), job("C")]
    assert asyncio.run(guess.levels(abc)) == {job("A").dedup_key: "pleno"}
    assert fake.calls == [["A", "B"]]  # teto de 2: a C fica para outros
    # A e B já foram respondidas (B = indefinido): não pergunta de novo; teto acabou
    asyncio.run(guess.levels(abc))
    assert fake.calls == [["A", "B"]]
    day[0] = 2  # dia novo: teto e memória zeram
    assert asyncio.run(guess.levels([job("C")])) == {job("C").dedup_key: "junior"}
    assert fake.calls[-1] == ["C"]


def test_level_llm_batches_at_most_llm_batch(monkeypatch):
    monkeypatch.setattr(canal, "LLM_BATCH", 2)
    fake = FakeLevels()
    asyncio.run(llm(fake, per_day=10).levels([job(t) for t in "ABC"]))
    assert fake.calls == [["A", "B"]]


def test_level_llm_fatal_error_stops_for_the_day_and_transient_retries():
    fatal = FakeLevels(error=FatalScoringError("sem crédito"))
    guess = llm(fatal)
    assert asyncio.run(guess.levels([job("A")])) == {}
    asyncio.run(guess.levels([job("B")]))
    assert fatal.calls == [["A"]]

    flaky = FakeLevels(error=TimeoutError())
    guess = llm(flaky)
    asyncio.run(guess.levels([job("A")]))
    flaky.error, flaky.answers = None, {"A": "pleno"}
    assert asyncio.run(guess.levels([job("A")])) == {job("A").dedup_key: "pleno"}


def test_cycle_asks_the_llm_only_for_jobs_the_rules_could_not_place(tmp_path, monkeypatch):
    monkeypatch.setattr(canal, "POST_GAP", 0)
    s = settings(tmp_path)
    s.searches_file.write_text(
        s.searches_file.read_text(encoding="utf-8")
        + "discord:\n  threads: {estagio: 11, pleno: 33, outros: 99}\n", encoding="utf-8")
    source = FakeSource([job("Estágio Dev"), job("Analista de TI"), job("Analista de BI")])
    fake = FakeLevels({"Analista de TI": "pleno"})
    sent = {}

    async def post(url, row, thread_id=None):
        sent[row["title"]] = (thread_id, row["nivel"])
        return True

    asyncio.run(cycle(s, Seen(days=7), post=post, sources=[source], llm=llm(fake)))
    assert fake.calls == [["Analista de TI", "Analista de BI"]]
    assert sent == {"Estágio Dev": ("11", "Estágio pelo título"),
                    "Analista de TI": ("33", "Pleno pela IA"),
                    "Analista de BI": ("99", "nível não identificado")}
