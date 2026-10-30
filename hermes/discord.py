"""Cópia dos cards num canal do Discord, por webhook.

Só posta: aprovar, descartar e marcar continuam no Telegram. O canal é aberto
aos amigos do Gabriel, então motivos e alertas da nota (que citam o perfil dele)
ficam de fora. A URL do webhook é segredo (quem tem posta no canal), então nunca
entra em log nem em mensagem de erro.
"""

import json
import logging

import httpx

log = logging.getLogger(__name__)

# Cor da barra lateral do embed pela recomendação.
COLORS = {"aplicar": 0x2ECC71, "talvez": 0xF1C40F}
DEFAULT_COLOR = 0x95A5A6


def card_embed(row) -> dict:
    """O card como embed. O Discord não interpreta HTML; markdown só na descrição."""
    lines = [f"{row['company']} · {row['location'] or 'local não informado'}"]
    color = DEFAULT_COLOR
    if row["score_reason"]:
        s = json.loads(row["score_reason"])
        color = COLORS.get(s["recomendacao"], DEFAULT_COLOR)
        lines.append(
            f"\n**Nota {row['score']}** · {s['recomendacao'].replace('_', ' ')} · "
            f"{s['nivel_detectado']} · {s['modelo_trabalho']}"
        )
    return {
        "title": row["title"][:256],
        "url": row["url"],
        "description": "\n".join(lines)[:4096],
        "color": color,
        # No modo canal a vaga não tem id: nada é guardado.
        "footer": {"text": f"#{row['id']} · {row['source']}" if row["id"] else row["source"]},
    }


async def _send(webhook_url: str, payload: dict, client: httpx.AsyncClient | None) -> int:
    if client is None:
        async with httpx.AsyncClient(timeout=15) as c:
            return (await c.post(webhook_url, json=payload)).status_code
    return (await client.post(webhook_url, json=payload)).status_code


async def post_card(webhook_url: str, row, client: httpx.AsyncClient | None = None) -> bool:
    """Posta o card. Devolve False se falhou; falha aqui nunca derruba o ciclo."""
    # Sem menções: um título de vaga com @everyone não pinga o servidor.
    embed = card_embed(row)
    payload = {"embeds": [embed], "allowed_mentions": {"parse": []}}
    label = f"#{row['id']}" if row["id"] else repr(row["title"][:60])
    try:
        status = await _send(webhook_url, payload, client)
        if status == 400:
            # O Discord recusa link com "_" no domínio (ex.: novostalentos_x.gupy.io).
            # Segunda tentativa sem o título clicável, com o link no texto.
            embed["description"] = f"{embed['description']}\n{embed.pop('url')}"[:4096]
            status = await _send(webhook_url, payload, client)
    except httpx.HTTPError as exc:
        # A mensagem do httpx traz a URL; só o tipo do erro vai para o log.
        log.warning("Discord: falha ao postar a vaga %s (%s)", label, type(exc).__name__)
        return False
    if status >= 400:
        log.warning("Discord: HTTP %s ao postar a vaga %s", status, label)
        return False
    return True
