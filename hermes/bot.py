"""Bot do Telegram: cards com botões, comandos e o agendamento.

Long polling — o bot busca as mensagens, nada precisa ser exposto na internet.
Só responde ao TELEGRAM_CHAT_ID configurado; qualquer outra conversa é ignorada.
"""

import asyncio
import html
import json
import logging
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    filters,
)

from hermes.config import Settings
from hermes.db import Database
from hermes.jobs import make_scorer_from, run_cycle, sweep_no_reply
from hermes.mail import ICONS, GmailInbox, apply_to_job, check_mail
from hermes.models import InvalidTransition, Status

log = logging.getLogger(__name__)

STATUS_LABEL = {
    Status.AGUARDANDO_VOCE: "🕐 esperando você",
    Status.APROVADA: "✅ vai aplicar",
    Status.APLICADA: "📨 aplicada",
    Status.EM_PROCESSO: "🎯 em processo",
    Status.CONTRATADO: "🎉 contratado",
    Status.RECUSADA: "🚫 recusada",
    Status.DESCARTADA: "❌ descartada",
    Status.SEM_RESPOSTA: "🔇 sem resposta",
}

HELP = """<b>Vagas do Vascon</b>
Eu busco vagas a cada {every} h, dou nota e mando aqui as que valem a pena.

/pendentes — vagas esperando você e as que você vai aplicar
/aplicadas — candidaturas em andamento
/buscar — roda uma busca agora
/resumo — quantas vagas em cada estado
"""


# --- apresentação ------------------------------------------------------------


def card_text(row) -> str:
    e = html.escape
    lines = [f"<b>{e(row['title'])}</b>", f"{e(row['company'])} · {e(row['location'] or 'local não informado')}"]
    if row["score_reason"]:
        s = json.loads(row["score_reason"])
        lines.append(
            f"\n<b>Nota {row['score']}</b> · {s['recomendacao'].replace('_', ' ')} · "
            f"{s['nivel_detectado']} · {s['modelo_trabalho']}"
        )
        lines += [f"➕ {e(m)}" for m in s["motivos"]]
        lines += [f"⚠️ {e(a)}" for a in s["alertas"]]
    lines.append(f"\n<i>{STATUS_LABEL.get(Status(row['status']), row['status'])} · #{row['id']} · {row['source']}</i>")
    return "\n".join(lines)


def card_keyboard(row) -> InlineKeyboardMarkup:
    job_id, status = row["id"], Status(row["status"])
    link = [InlineKeyboardButton("🔗 Abrir vaga", url=row["url"])]
    if status == Status.AGUARDANDO_VOCE:
        rows = [[InlineKeyboardButton("✅ Vou aplicar", callback_data=f"ok:{job_id}"),
                 InlineKeyboardButton("❌ Descartar", callback_data=f"no:{job_id}")],
                [InlineKeyboardButton("📨 Já apliquei", callback_data=f"ap:{job_id}")], link]
    elif status == Status.APROVADA:
        rows = [[InlineKeyboardButton("📨 Apliquei", callback_data=f"ap:{job_id}"),
                 InlineKeyboardButton("❌ Desisti", callback_data=f"no:{job_id}")], link]
    else:
        rows = [link]
    return InlineKeyboardMarkup(rows)


# O que cada botão faz: novo estado e a decisão que ensina a nota.
ACTIONS = {
    "ok": (Status.APROVADA, "aprovou"),
    "ap": (Status.APLICADA, "aprovou"),
    "no": (Status.DESCARTADA, "descartou"),
}


def apply_action(db: Database, action: str, job_id: int) -> str | None:
    """Aplica o botão. Devolve None se deu certo, ou o motivo de não ter feito nada."""
    new, decision = ACTIONS[action]
    current = Status(db.get(job_id)["status"])
    try:
        db.transition(job_id, new, "pelo Telegram")
    except InvalidTransition:
        return f"Essa vaga já está como {STATUS_LABEL.get(current, current)}."
    db.set_decision(job_id, decision)
    return None


# --- handlers ----------------------------------------------------------------


def _db(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data["db"]


def _settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.application.bot_data["settings"]


async def send_card(context, chat_id: int, row) -> None:
    await context.bot.send_message(
        chat_id, card_text(row), parse_mode=ParseMode.HTML,
        reply_markup=card_keyboard(row), disable_web_page_preview=True,
    )


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if update.effective_chat.id != _settings(context).telegram_chat_id:
        await query.answer()
        return
    if query.data.startswith("m:"):
        await on_mail_button(update, context)
        return
    action, _, raw_id = query.data.partition(":")
    db = _db(context)
    problem = apply_action(db, action, int(raw_id))
    await query.answer(problem or "Anotado!")
    row = db.get(int(raw_id))
    await query.edit_message_text(
        card_text(row), parse_mode=ParseMode.HTML,
        reply_markup=card_keyboard(row), disable_web_page_preview=True,
    )


# --- e-mail -------------------------------------------------------------------


def notice_text(db: Database, n) -> str:
    e = html.escape
    icon = ICONS.get(n.kind, "✉️")
    lines = [f"{icon} <b>{n.kind.replace('_', ' ').capitalize()}</b>"]
    if n.job_id is not None:
        job = db.get(n.job_id)
        lines.append(f"{e(job['title'])} — {e(job['company'])}")
    lines.append(e(n.summary))
    if n.deadline:
        lines.append(f"⏰ <b>Prazo:</b> {e(n.deadline)}")
    if n.job_id is not None:
        job = db.get(n.job_id)
        lines.append(f"\n<i>{STATUS_LABEL.get(Status(job['status']), job['status'])} · #{job['id']}</i>")
    return "\n".join(lines)


def link_keyboard(db: Database, mail_id: int) -> InlineKeyboardMarkup:
    """Para e-mail sem vaga ligada: um botão por candidatura ativa, mais ignorar."""
    rows = [[InlineKeyboardButton(f"#{r['id']} {r['company'][:28]}", callback_data=f"m:{mail_id}:{r['id']}")]
            for r in db.active_applications()[:8]]
    rows.append([InlineKeyboardButton("🙈 Não é de nenhuma", callback_data=f"m:{mail_id}:0")])
    return InlineKeyboardMarkup(rows)


async def on_mail_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, raw_mail, raw_job = query.data.split(":")
    db = _db(context)
    mail = db.get_mail(int(raw_mail))
    if mail["job_id"] is not None:
        await query.answer("Esse e-mail já está ligado a uma vaga.")
        return
    if raw_job == "0":
        await query.answer("Ok, ignorado.")
        await query.edit_message_reply_markup(None)
        return
    job_id = int(raw_job)
    db.link_mail(mail["id"], job_id)
    apply_to_job(db, job_id, mail["kind"], mail["summary"])
    await query.answer("Ligado!")
    job = db.get(job_id)
    await query.edit_message_text(
        f"{query.message.text_html}\n\n✅ Ligado a <b>{html.escape(job['title'])}</b> — "
        f"{html.escape(job['company'])} ({STATUS_LABEL.get(Status(job['status']))})",
        parse_mode=ParseMode.HTML,
    )


async def mail_cycle(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.application.bot_data
    settings = data["settings"]
    if data.get("mail_running") or not settings.mail_enabled:
        return
    data["mail_running"] = True
    try:
        notices = await asyncio.to_thread(_check_mail_sync, settings)
    except Exception as exc:
        log.exception("leitura do e-mail falhou")
        # Não avisa a cada 15 min pelo mesmo erro: só na primeira vez seguida.
        if not data.get("mail_error"):
            await context.bot.send_message(settings.telegram_chat_id, f"⚠️ Não consegui ler o e-mail: {exc}")
        data["mail_error"] = True
        return
    finally:
        data["mail_running"] = False
    data["mail_error"] = False
    db = _db(context)
    for n in notices:
        markup = link_keyboard(db, n.mail_id) if n.job_id is None else None
        text = notice_text(db, n)
        if n.job_id is None:
            text += "\n\n<b>De qual candidatura é?</b>"
        await context.bot.send_message(settings.telegram_chat_id, text, parse_mode=ParseMode.HTML,
                                       reply_markup=markup)


def _check_mail_sync(settings: Settings):
    llm = make_scorer_from(settings)
    if llm is None:
        return []
    inbox = GmailInbox(settings.gmail_address, settings.gmail_app_password,
                       settings.mail_only_to, settings.mail_first_run_days)
    # conexão própria: roda fora da thread do bot
    return check_mail(Database(settings.db_path), inbox, llm)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_html(HELP.format(every=_settings(context).search_every_hours))


async def cmd_pendentes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db = _db(context)
    rows = db.by_status(Status.APROVADA) + sorted(
        db.by_status(Status.AGUARDANDO_VOCE), key=lambda r: -(r["score"] or 0)
    )
    if not rows:
        await update.message.reply_text("Nada pendente. 🎈")
        return
    for row in rows:
        await send_card(context, update.effective_chat.id, row)
        db.mark_notified(row["id"])


async def cmd_aplicadas(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db = _db(context)
    rows = db.by_status(Status.APLICADA) + db.by_status(Status.EM_PROCESSO)
    if not rows:
        await update.message.reply_text("Nenhuma candidatura em andamento.")
        return
    now = datetime.now(UTC)
    lines = []
    for r in sorted(rows, key=lambda r: r["status_at"]):
        days = (now - datetime.fromisoformat(r["status_at"])).days
        lines.append(f"• {html.escape(r['title'])} — {html.escape(r['company'])}\n"
                     f"  {STATUS_LABEL[Status(r['status'])]} há {days} d · #{r['id']}")
    await update.message.reply_html("\n".join(lines), disable_web_page_preview=True)


async def cmd_resumo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_html(summary_text(_db(context)))


def summary_text(db: Database) -> str:
    counts = db.counts()
    order = [Status.AGUARDANDO_VOCE, Status.APROVADA, Status.APLICADA, Status.EM_PROCESSO,
             Status.CONTRATADO, Status.RECUSADA, Status.SEM_RESPOSTA, Status.DESCARTADA]
    lines = [f"{STATUS_LABEL[s]}: <b>{counts[s]}</b>" for s in order if counts.get(s)]
    return "\n".join(lines) or "Banco vazio."


async def cmd_buscar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.application.bot_data.get("running"):
        await update.message.reply_text("Já estou buscando, espera um pouco. ⏳")
        return
    await update.message.reply_text("Buscando… costuma levar uns minutos. ⏳")
    await cycle(context, announce_empty=True)


# --- agendados -----------------------------------------------------------------


async def cycle(context: ContextTypes.DEFAULT_TYPE, announce_empty: bool = False) -> None:
    """Busca + nota em uma thread (JobSpy e os SDKs de LLM são síncronos) e avisa."""
    data = context.application.bot_data
    if data.get("running"):
        return
    data["running"] = True
    settings, chat_id = data["settings"], data["settings"].telegram_chat_id
    try:
        result = await asyncio.to_thread(run_cycle, settings)
    except Exception as exc:
        log.exception("ciclo falhou")
        await context.bot.send_message(chat_id, f"⚠️ A busca falhou: {exc}")
        return
    finally:
        data["running"] = False

    db = _db(context)
    new_cards = db.to_notify()
    for row in new_cards:
        await send_card(context, chat_id, row)
        db.mark_notified(row["id"])

    problems = result.search.errors + (result.evaluation.errors if result.evaluation else [])
    if problems:
        text = "\n".join(f"• {html.escape(p[:300])}" for p in problems[:5])
        await context.bot.send_message(chat_id, f"⚠️ Problemas na busca:\n{text}", parse_mode=ParseMode.HTML)
    if announce_empty and not new_cards:
        await context.bot.send_message(
            chat_id, f"Nenhuma vaga nova que valha a pena ({len(result.search.new_ids)} novas encontradas)."
        )


async def scheduled_cycle(context: ContextTypes.DEFAULT_TYPE) -> None:
    await cycle(context)


async def daily(context: ContextTypes.DEFAULT_TYPE) -> None:
    db = _db(context)
    chat_id = _settings(context).telegram_chat_id
    for job_id in sweep_no_reply(db):
        row = db.get(job_id)
        await context.bot.send_message(
            chat_id, f"🔇 90 dias sem resposta: <b>{html.escape(row['title'])}</b> — "
                     f"{html.escape(row['company'])}. Marquei como sem resposta.",
            parse_mode=ParseMode.HTML,
        )
    counts = db.counts()
    waiting = counts.get(Status.AGUARDANDO_VOCE, 0) + counts.get(Status.APROVADA, 0)
    if waiting:
        await context.bot.send_message(
            chat_id, f"☀️ Bom dia! {waiting} vaga(s) esperando você — /pendentes\n\n" + summary_text(db),
            parse_mode=ParseMode.HTML,
        )


async def heartbeat(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toca data/heartbeat: o healthcheck do Docker considera o bot vivo por ele."""
    (_settings(context).data_dir / "heartbeat").touch()


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    from telegram.error import NetworkError, TimedOut

    # Queda de rede no long polling é normal e a biblioteca reconecta sozinha.
    if isinstance(context.error, (NetworkError, TimedOut)):
        log.warning("rede instável com o Telegram: %s", context.error)
        return
    log.error("erro no bot", exc_info=context.error)


# --- montagem -----------------------------------------------------------------


def build_app(settings: Settings) -> Application:
    if not (settings.telegram_token and settings.telegram_chat_id):
        raise SystemExit("TELEGRAM_TOKEN e TELEGRAM_CHAT_ID precisam estar no .env")
    app = Application.builder().token(settings.telegram_token).build()
    app.bot_data["settings"] = settings
    app.bot_data["db"] = Database(settings.db_path)

    only_me = filters.Chat(chat_id=settings.telegram_chat_id)
    app.add_handler(CommandHandler(["start", "ajuda", "help"], cmd_help, filters=only_me))
    app.add_handler(CommandHandler("pendentes", cmd_pendentes, filters=only_me))
    app.add_handler(CommandHandler("aplicadas", cmd_aplicadas, filters=only_me))
    app.add_handler(CommandHandler("resumo", cmd_resumo, filters=only_me))
    app.add_handler(CommandHandler("buscar", cmd_buscar, filters=only_me, block=False))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_error_handler(on_error)

    tz = ZoneInfo(settings.timezone)
    app.job_queue.run_repeating(
        scheduled_cycle, interval=settings.search_every_hours * 3600, first=30, name="ciclo"
    )
    app.job_queue.run_daily(daily, time=time(settings.digest_hour, 0, tzinfo=tz), name="diario")
    app.job_queue.run_repeating(heartbeat, interval=60, first=1, name="heartbeat")
    if settings.mail_enabled:
        app.job_queue.run_repeating(
            mail_cycle, interval=settings.mail_every_minutes * 60, first=60, name="email"
        )
    return app


def run(settings: Settings) -> None:
    build_app(settings).run_polling(allowed_updates=["message", "callback_query"])
