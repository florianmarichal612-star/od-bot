"""
Telegram-бот для автоматической сборки еженедельного отчёта ОД
Сеть «Хачапури Марико»

Логика:
  Пятница 09:00 → бот рассылает форму команде (РШ, КМ, маркетолог)
  Пятница 16:00 → бот собирает KPI из iiko + задачи из Bitrix24
  Пятница 16:30 → бот пишет ОД: «Добавь итоговый комментарий»
  ОД отвечает   → бот генерирует отчёт через Claude и отправляет директору
"""

import os
import asyncio
import logging
from datetime import datetime, timedelta
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import anthropic

# ─────────────────────────────────────────────
# НАСТРОЙКИ (берутся из переменных окружения)
# ─────────────────────────────────────────────
TELEGRAM_TOKEN     = os.environ["TELEGRAM_TOKEN"]        # токен бота от @BotFather
ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]     # ключ Claude API

# ID телеграм-аккаунтов (узнать через @userinfobot)
OD_CHAT_ID         = int(os.environ["OD_CHAT_ID"])       # операционный директор
DIRECTOR_CHAT_ID   = int(os.environ["DIRECTOR_CHAT_ID"]) # директор по развитию

# Bitrix24
BITRIX_WEBHOOK     = os.environ["BITRIX_WEBHOOK"]        # https://yourcompany.bitrix24.ru/rest/1/xxxxx/
BITRIX_OD_USER_ID  = os.environ.get("BITRIX_OD_USER_ID", "1")  # ID пользователя ОД в Bitrix24

# iiko облако
IIKO_LOGIN         = os.environ["IIKO_LOGIN"]            # логин в iiko.biz
IIKO_PASSWORD      = os.environ["IIKO_PASSWORD"]         # пароль
IIKO_ORG_IDS       = os.environ["IIKO_ORG_IDS"]         # ID организаций через запятую

# Telegram ID членов команды (опционально — если не заданы, бот пропускает рассылку)
TEAM_IDS = {
    "РШ (региональный шеф)":  int(os.environ.get("RSH_CHAT_ID",  "0")),
    "Клиент-менеджер":         int(os.environ.get("KM_CHAT_ID",   "0")),
    "Маркетолог":              int(os.environ.get("MKT_CHAT_ID",  "0")),
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# ХРАНИЛИЩЕ СОСТОЯНИЯ (в памяти, достаточно для MVP)
# ─────────────────────────────────────────────
state = {
    "team_reports": {},   # role → text
    "od_comment": "",
    "waiting_od_comment": False,
    "iiko_data": [],
    "bitrix_tasks": [],
}


# ─────────────────────────────────────────────
# IIKO — получение KPI
# ─────────────────────────────────────────────
async def fetch_iiko_kpi() -> list[dict]:
    """Возвращает список точек с показателями за текущую неделю."""
    try:
        org_ids = [x.strip() for x in IIKO_ORG_IDS.split(",") if x.strip()]
        today = datetime.now()
        monday = today - timedelta(days=today.weekday())
        date_from = monday.strftime("%Y-%m-%d")
        date_to   = today.strftime("%Y-%m-%d")

        async with httpx.AsyncClient(timeout=20) as client:
            # 1. Получаем токен сессии
            auth = await client.get(
                "https://api-ru.iiko.services/api/1/access_token",
                params={"login": IIKO_LOGIN, "pass": IIKO_PASSWORD}
            )
            token = auth.json().get("token", "")
            if not token:
                log.warning("iiko: не удалось получить токен")
                return []

            headers = {"Authorization": f"Bearer {token}"}
            results = []

            for org_id in org_ids:
                # 2. Запрашиваем выручку
                r = await client.post(
                    "https://api-ru.iiko.services/api/1/reports/olap",
                    headers=headers,
                    json={
                        "reportType": "SALES",
                        "buildSummary": "false",
                        "groupByRowFields": ["Department"],
                        "aggregateFields": [
                            "DishSumInt", "GuestsCount", "OrdersCount"
                        ],
                        "filters": {
                            "OpenDate.Typed": {
                                "filterType": "DateRange",
                                "periodType": "CUSTOM",
                                "customBegin": f"{date_from}T00:00:00",
                                "customEnd":   f"{date_to}T23:59:59",
                            },
                            "Organization": {
                                "filterType": "InFilter",
                                "values": [org_id]
                            }
                        },
                        "organizationIds": [org_id]
                    }
                )
                data = r.json()
                rows = data.get("data", [])

                for row in rows:
                    revenue   = row.get("DishSumInt", 0)
                    guests    = row.get("GuestsCount", 0)
                    avg_check = round(revenue / guests, 0) if guests else 0
                    results.append({
                        "name":      row.get("Department", org_id),
                        "revenue":   f"{int(revenue):,} ₽".replace(",", " "),
                        "guests":    str(guests),
                        "avg_check": f"{int(avg_check)} ₽",
                        "margin":    "—",   # наценка в iiko требует отдельного отчёта
                    })

            return results

    except Exception as e:
        log.error(f"iiko error: {e}")
        return []


# ─────────────────────────────────────────────
# BITRIX24 — получение задач ОД за неделю
# ─────────────────────────────────────────────
async def fetch_bitrix_tasks() -> list[dict]:
    """Возвращает задачи ОД за текущую неделю."""
    try:
        today   = datetime.now()
        monday  = today - timedelta(days=today.weekday())
        date_from = monday.strftime("%Y-%m-%dT00:00:00")

        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                f"{BITRIX_WEBHOOK.rstrip('/')}/tasks.task.list",
                json={
                    "filter": {
                        "::LOGIC": "OR",
                        "RESPONSIBLE_ID":  BITRIX_OD_USER_ID,
                        "CREATED_BY":      BITRIX_OD_USER_ID,
                        "AUDITOR":         BITRIX_OD_USER_ID,
                        "ACCOMPLICE":      BITRIX_OD_USER_ID,
                        ">=CREATED_DATE":  date_from,
                    },
                    "select": ["ID", "TITLE", "STATUS", "DEADLINE", "CREATED_BY", "RESPONSIBLE_ID"],
                    "order":  {"DEADLINE": "ASC"},
                }
            )
            data = r.json()
            tasks = data.get("result", {}).get("tasks", [])

            STATUS_MAP = {
                "1": "⏳ Ждёт выполнения",
                "2": "⏳ В работе",
                "3": "✅ Выполнено",
                "4": "⚠️ Ожидает контроля",
                "5": "❌ Просрочена",
                "6": "➡️ Отложена",
            }

            result = []
            for t in tasks:
                deadline = t.get("deadline", "")
                if deadline:
                    try:
                        deadline = datetime.fromisoformat(deadline).strftime("%d.%m")
                    except Exception:
                        pass
                result.append({
                    "title":    t.get("title", ""),
                    "status":   STATUS_MAP.get(t.get("status", "2"), "В работе"),
                    "deadline": deadline,
                })
            return result

    except Exception as e:
        log.error(f"bitrix error: {e}")
        return []


# ─────────────────────────────────────────────
# CLAUDE — генерация отчёта
# ─────────────────────────────────────────────
async def generate_report_text() -> tuple[str, str]:
    """Возвращает (полный_отчёт, краткий_для_мессенджера)."""

    iiko  = state["iiko_data"]
    tasks = state["bitrix_tasks"]
    team  = state["team_reports"]
    comment = state["od_comment"]

    period_end   = datetime.now()
    period_start = period_end - timedelta(days=4)
    period_str   = f"{period_start.strftime('%d')} — {period_end.strftime('%d %B %Y')}"

    kpi_lines = "\n".join(
        f"  • {p['name']}: выручка {p['revenue']}, гости {p['guests']}, ср.чек {p['avg_check']}"
        for p in iiko
    ) or "  Данные из iiko не получены"

    task_lines = "\n".join(
        f"  • [{t['status']}] {t['title']} (дедлайн: {t['deadline']})"
        for t in tasks
    ) or "  Задачи из Bitrix24 не получены"

    team_lines = "\n".join(
        f"  • {role}: {text}"
        for role, text in team.items()
    ) or "  Отчёты от команды не поступили"

    prompt = f"""Ты — профессиональный операционный директор сети ресторанов «Хачапури Марико».
Составь два варианта еженедельного отчёта на основе данных ниже.

ПЕРИОД: {period_str}

KPI ПАРТНЁРОВ (из iiko):
{kpi_lines}

ЗАДАЧИ НЕДЕЛИ (из Bitrix24):
{task_lines}

ОТЧЁТЫ КОМАНДЫ:
{team_lines}

КОММЕНТАРИЙ ОД:
{comment or 'Не добавлен'}

ИНСТРУКЦИИ:
Напиши профессиональный деловой отчёт с чёткими разделами.
Обозначь проблемные точки и сформулируй конкретные выводы.
Если данных по какому-то разделу нет — напиши «данные уточняются».

Оберни полный отчёт в [FULL]...[/FULL]
Оберни краткую версию для Telegram в [SHORT]...[/SHORT]
Краткая версия — максимум 25 строк, с эмодзи-маркерами."""

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}]
    )
    text = message.content[0].text

    import re
    full_match  = re.search(r"\[FULL\]([\s\S]*?)\[/FULL\]",  text)
    short_match = re.search(r"\[SHORT\]([\s\S]*?)\[/SHORT\]", text)

    full  = full_match.group(1).strip()  if full_match  else text
    short = short_match.group(1).strip() if short_match else ""
    return full, short


# ─────────────────────────────────────────────
# РАСПИСАНИЕ — пятничные задачи
# ─────────────────────────────────────────────
async def job_collect_team(app: Application):
    """09:00 пятницы — рассылаем форму команде."""
    for role, chat_id in TEAM_IDS.items():
        if chat_id == 0:
            continue
        try:
            await app.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"👋 Привет! Пятница — время короткого отчёта.\n\n"
                    f"Ты в роли: *{role}*\n\n"
                    f"Пришли одним сообщением:\n"
                    f"• Что выполнено за неделю\n"
                    f"• Что в работе / перенесено\n"
                    f"• Есть ли проблемы или отклонения\n\n"
                    f"_Дедлайн: 15:00_"
                ),
                parse_mode="Markdown"
            )
        except Exception as e:
            log.error(f"Не удалось написать {role}: {e}")


async def job_collect_kpi_and_notify_od(app: Application):
    """16:00 пятницы — собираем iiko + Bitrix24, пишем ОД."""
    log.info("Сбор данных из iiko и Bitrix24...")
    state["iiko_data"]    = await fetch_iiko_kpi()
    state["bitrix_tasks"] = await fetch_bitrix_tasks()

    missing_team = [r for r, cid in TEAM_IDS.items() if cid != 0 and r not in state["team_reports"]]
    missing_note = ""
    if missing_team:
        missing_note = f"\n\n⚠️ Не прислали отчёт: {', '.join(missing_team)}"

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✍️ Добавить комментарий", callback_data="add_comment"),
        InlineKeyboardButton("⏭ Пропустить", callback_data="skip_comment"),
    ]])

    await app.bot.send_message(
        chat_id=OD_CHAT_ID,
        text=(
            f"📋 *Данные за неделю собраны.*{missing_note}\n\n"
            f"Из iiko: {len(state['iiko_data'])} точек\n"
            f"Из Bitrix24: {len(state['bitrix_tasks'])} задач\n"
            f"Отчёты команды: {len(state['team_reports'])}/{len([c for c in TEAM_IDS.values() if c != 0])}\n\n"
            f"Добавь итоговый комментарий (голосом или текстом) — или пропусти."
        ),
        parse_mode="Markdown",
        reply_markup=keyboard
    )
    state["waiting_od_comment"] = False


# ─────────────────────────────────────────────
# ОБРАБОТЧИКИ TELEGRAM
# ─────────────────────────────────────────────
async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "add_comment":
        state["waiting_od_comment"] = True
        await query.edit_message_text(
            "✍️ Напиши итоговый комментарий:\n\n"
            "• Что прошло хорошо\n"
            "• Что требует внимания\n"
            "• Запрос к директору по развитию\n\n"
            "_Можно одним сообщением в свободной форме_",
            parse_mode="Markdown"
        )

    elif query.data == "skip_comment":
        state["od_comment"] = ""
        await query.edit_message_text("⏭ Комментарий пропущен. Генерирую отчёт...")
        await do_generate(ctx.application)

    elif query.data == "confirm_send":
        full, short = state.get("_last_report", ("", ""))
        await ctx.application.bot.send_message(
            chat_id=DIRECTOR_CHAT_ID,
            text=f"📬 *Еженедельный отчёт ОД*\n\n{short or full}",
            parse_mode="Markdown"
        )
        await query.edit_message_text("✅ Отчёт отправлен директору по развитию.")
        # Сброс состояния
        state["team_reports"] = {}
        state["od_comment"] = ""

    elif query.data == "edit_report":
        state["waiting_od_comment"] = True
        await query.edit_message_text(
            "✏️ Напиши правки или дополнения — перегенерирую отчёт с ними:",
            parse_mode="Markdown"
        )


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_chat.id
    text = update.message.text or (update.message.voice and "[голосовое сообщение]") or ""

    # Сообщение от члена команды
    for role, chat_id in TEAM_IDS.items():
        if chat_id != 0 and user_id == chat_id:
            state["team_reports"][role] = text
            await update.message.reply_text(
                f"✅ Принято! Твой отчёт сохранён.\nОН получит всё вместе в конце дня."
            )
            return

    # Комментарий от ОД
    if user_id == OD_CHAT_ID and state["waiting_od_comment"]:
        state["od_comment"] = text
        state["waiting_od_comment"] = False
        await update.message.reply_text("💬 Комментарий сохранён. Генерирую отчёт...")
        await do_generate(ctx.application)
        return

    # Неизвестное сообщение от ОД
    if user_id == OD_CHAT_ID:
        await update.message.reply_text(
            "Привет! Используй /status чтобы посмотреть статус сбора данных, "
            "или /generate чтобы сгенерировать отчёт вручную."
        )


async def do_generate(app: Application):
    """Генерирует отчёт и отправляет ОД на подтверждение."""
    try:
        await app.bot.send_message(OD_CHAT_ID, "⏳ Генерирую отчёт, подожди 10–15 секунд...")
        full, short = await generate_report_text()
        state["_last_report"] = (full, short)

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Отправить директору", callback_data="confirm_send"),
            InlineKeyboardButton("✏️ Добавить правки",    callback_data="edit_report"),
        ]])

        # Отправляем полный отчёт (разбиваем если > 4096 символов)
        chunks = [full[i:i+4000] for i in range(0, len(full), 4000)]
        for i, chunk in enumerate(chunks):
            if i == len(chunks) - 1:
                await app.bot.send_message(OD_CHAT_ID, chunk, reply_markup=keyboard)
            else:
                await app.bot.send_message(OD_CHAT_ID, chunk)

    except Exception as e:
        log.error(f"generate error: {e}")
        await app.bot.send_message(OD_CHAT_ID, f"❌ Ошибка генерации: {e}")


# ─────────────────────────────────────────────
# КОМАНДЫ
# ─────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Бот еженедельного отчёта ОД*\n\n"
        "Каждую пятницу автоматически:\n"
        "• 09:00 — рассылаю форму команде\n"
        "• 16:00 — собираю KPI из iiko и задачи из Bitrix24\n"
        "• 16:30 — прошу тебя добавить комментарий\n"
        "• После ответа — генерирую и отправляю директору\n\n"
        "Команды:\n"
        "/generate — сгенерировать отчёт прямо сейчас\n"
        "/status — статус сбора данных\n"
        "/collect — вручную запустить сбор из iiko и Bitrix24",
        parse_mode="Markdown"
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    team_done   = list(state["team_reports"].keys())
    team_miss   = [r for r in TEAM_IDS if TEAM_IDS[r] != 0 and r not in state["team_reports"]]
    iiko_count  = len(state["iiko_data"])
    tasks_count = len(state["bitrix_tasks"])

    text = (
        f"📊 *Статус сбора данных*\n\n"
        f"iiko (точки): {iiko_count} {'✅' if iiko_count else '❌ не загружено'}\n"
        f"Bitrix24 (задачи): {tasks_count} {'✅' if tasks_count else '❌ не загружено'}\n\n"
        f"Команда:\n"
    )
    for r in (list(TEAM_IDS.keys())):
        if TEAM_IDS[r] == 0:
            text += f"  • {r}: не настроен\n"
        elif r in team_done:
            text += f"  • {r}: ✅ отчёт получен\n"
        else:
            text += f"  • {r}: ⏳ ожидаем\n"

    if state["od_comment"]:
        text += f"\nКомментарий ОД: ✅ добавлен"
    else:
        text += f"\nКомментарий ОД: ⏳ не добавлен"

    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_collect(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 Собираю данные из iiko и Bitrix24...")
    state["iiko_data"]    = await fetch_iiko_kpi()
    state["bitrix_tasks"] = await fetch_bitrix_tasks()
    await update.message.reply_text(
        f"✅ Готово:\n"
        f"• iiko: {len(state['iiko_data'])} точек\n"
        f"• Bitrix24: {len(state['bitrix_tasks'])} задач"
    )


async def cmd_generate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != OD_CHAT_ID:
        return
    if not state["iiko_data"] and not state["bitrix_tasks"]:
        await update.message.reply_text("Сначала собери данные: /collect")
        return
    await do_generate(ctx.application)


# ─────────────────────────────────────────────
# ЗАПУСК
# ─────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("status",   cmd_status))
    app.add_handler(CommandHandler("collect",  cmd_collect))
    app.add_handler(CommandHandler("generate", cmd_generate))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT | filters.VOICE, handle_message))

    # Расписание
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
    scheduler.add_job(
        lambda: asyncio.create_task(job_collect_team(app)),
        trigger="cron", day_of_week="fri", hour=9, minute=0
    )
    scheduler.add_job(
        lambda: asyncio.create_task(job_collect_kpi_and_notify_od(app)),
        trigger="cron", day_of_week="fri", hour=16, minute=0
    )
    scheduler.start()

    log.info("Бот запущен")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
