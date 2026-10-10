import asyncio
import json
import logging

import httpx

from hermes.db import Database
from hermes.discord import card_embed, post_card
from tests.test_bot import add

URL = "https://discord.com/api/webhooks/1/segredo"


def client_returning(status, seen):
    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(status)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_embed_is_plain_text_with_score_and_link():
    db = Database(":memory:")
    e = card_embed(db.get(add(db)))
    assert e["title"] == "Estágio Dev <Python>" and e["url"] == "https://acme.gupy.io/job/1"
    assert "Acme & Cia" in e["description"] and "**Nota 80**" in e["description"]
    assert "⚠️ prazo curto" in e["description"] and e["color"] == 0x2ECC71


def test_post_sends_embed_without_mentions():
    db = Database(":memory:")
    seen = []
    ok = asyncio.run(post_card(URL, db.get(add(db)), client_returning(204, seen)))
    assert ok and seen[0]["allowed_mentions"] == {"parse": []} and len(seen[0]["embeds"]) == 1


def test_failure_returns_false_and_never_logs_the_url(caplog):
    db = Database(":memory:")
    caplog.set_level(logging.WARNING)
    assert not asyncio.run(post_card(URL, db.get(add(db)), client_returning(404, [])))

    def boom(request):
        raise httpx.ConnectError(f"falhou {request.url}")
    broken = httpx.AsyncClient(transport=httpx.MockTransport(boom))
    assert not asyncio.run(post_card(URL, db.get(add(db)), broken))
    assert "segredo" not in caplog.text and "HTTP 404" in caplog.text
