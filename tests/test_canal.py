import asyncio
import json

import httpx

from hermes import canal
from hermes.canal import Seen, as_row, cycle, to_post
from hermes.config import Settings
from hermes.discord import post_card
from hermes.filters import FilterRules
from hermes.models import JobPosting

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

    async def post(url, row):
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
    row = as_row(job("Estágio", url="https://novos_talentos.gupy.io/job/1"))
    assert asyncio.run(post_card(URL, row, client))
    retry = sent[1]["embeds"][0]
    assert "url" not in retry and "https://novos_talentos.gupy.io/job/1" in retry["description"]
    assert retry["footer"]["text"] == "gupy"
