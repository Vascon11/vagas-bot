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
        "footer": {"text": f"#{row['id']} · {row['source']}"},
    }


async def post_card(webhook_url: str, row, client: httpx.AsyncClient | None = None) -> bool:
    """Posta o card. Devolve False se falhou; falha aqui nunca derruba o ciclo."""
    # Sem menções: um título de vaga com @everyone não pinga o servidor.
    payload = {"embeds": [card_embed(row)], "allowed_mentions": {"parse": []}}
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=15) as c:
                resp = await c.post(webhook_url, json=payload)
        else:
            resp = await client.post(webhook_url, json=payload)
    except httpx.HTTPError as exc:
        # A mensagem do httpx traz a URL; só o tipo do erro vai para o log.
        log.warning("Discord: falha ao postar a vaga #%s (%s)", row["id"], type(exc).__name__)
        return False
    if resp.status_code >= 400:
        log.warning("Discord: HTTP %s ao postar a vaga #%s", resp.status_code, row["id"])
        return False
    return True
