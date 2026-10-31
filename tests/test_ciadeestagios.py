import json
from datetime import UTC, date, datetime

import httpx

from hermes.sources.base import SearchQuery
from hermes.sources.ciadeestagios import SITEMAP, CiaDeEstagiosSource, places

NOW = datetime(2026, 10, 31, 12, tzinfo=UTC)
BASE = "https://www.ciadeestagios.com.br/vagas"


def page(body="Cursos: Ciência da Computação", **job):
    data = {"@type": "JobPosting", "title": "Programa de Estágio Acme 2027 | Companhia de Estágios",
            "hiringOrganization": {"name": "Acme"}, "employmentType": "INTERN",
            "datePosted": "2026-10-30", "validThrough": "2026-11-30",
            "jobLocation": [{"name": "São Paulo, SP"}], "description": "<p>Texto</p>", **job}
    return (f'<script type="application/ld+json">{json.dumps(data)}</script>'
            f"<style>.computacao{{}}</style><p>{body}</p>")


def source(pages, lastmod="2026-10-30T15:00:00+00:00"):
    xml = "".join(f"<url><loc>{BASE}/{k}/</loc><lastmod>{lastmod}</lastmod></url>" for k in pages)
    seen = []

    def handler(request):
        url = str(request.url)
        seen.append(url)
        if url == SITEMAP:
            return httpx.Response(200, text=f"<urlset>{xml}</urlset>")
        body = pages[url.rstrip("/").rsplit("/", 1)[-1]]
        return httpx.Response(404) if body is None else httpx.Response(200, text=body)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return CiaDeEstagiosSource(client, now=lambda: NOW, sleep=lambda s: None), seen


def query(**kw):
    return SearchQuery(name="cia", term="x", sites=("ciadeestagios",), hours_old=48, **kw)


def test_reads_the_json_ld_of_an_open_tech_program():
    src, _ = source({"acme": page()})
    p = src.search(query())[0]
    assert (p.title, p.company, p.location) == ("Programa de Estágio Acme 2027", "Acme", "São Paulo, SP")
    assert (p.source, p.external_id, p.url) == ("ciadeestagios", "acme", f"{BASE}/acme/")
    assert p.posted_at == date(2026, 10, 30) and p.level == "internship"


def test_skips_closed_old_non_tech_other_state_and_missing_pages():
    src, _ = source({
        "fechado": page(validThrough="2026-10-30"),
        "antigo": page(datePosted="2026-08-01"),
        "rh": page(body="Cursos: Administração"),
        "rj": page(jobLocation=[{"name": "Rio de Janeiro, RJ"}]),
        "semjson": "<p>Ciência da Computação</p>",
        "retirado": None,
    })
    assert src.search(query()) == []


def test_only_reads_pages_changed_in_the_window():
    src, seen = source({"acme": page()}, lastmod="2026-10-01T00:00:00+00:00")
    assert src.search(query()) == [] and seen == [SITEMAP]


def test_other_sites_do_not_touch_the_network():
    src, seen = source({})
    assert src.search(SearchQuery(name="x", term="x", sites=("gupy",))) == [] and seen == []


def test_location_from_address_and_multi_city_program():
    assert places({"jobLocation": [{"address": {"addressLocality": "Mauá", "addressRegion": "SP"}}]}) \
        == [("Mauá", "SP")]
    src, _ = source({"acme": page(jobLocation=[{"name": "Betim, MG"}, {"name": "Mauá, SP"}])})
    assert src.search(query())[0].location == "Mauá, SP"
