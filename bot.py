"""
Telegram-бот для автоматической сборки еженедельного отчёта ОД
Сеть «Хачапури Марико»
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
# Используем .get() чтобы бот не падал, если переменная случайно удалится
TELEGRAM_TOKEN     = os.environ.get("TELEGRAM_TOKEN", "")
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")

OD_CHAT_ID         = int(os.environ.get("OD_CHAT_ID", "0"))
DIRECTOR_CHAT_ID   = int(os.environ.get("DIRECTOR_CHAT_ID", "0"))

BITRIX_WEBHOOK     = os.environ.get("BITRIX_WEBHOOK", "")
BITRIX_OD_USER_ID  = os.environ.get("BITRIX_OD_USER_ID", "1")

IIKO_LOGIN         = os.environ.get("IIKO_LOGIN", "")
IIKO_PASSWORD      = os.environ.get("IIKO_PASSWORD", "")
IIKO_ORG_IDS       = os.environ.get("IIKO_ORG_IDS", "")

TEAM_IDS = {
    "РШ (региональный шеф)":  int(os.environ.get("RSH_CHAT_ID",  "0")),
    "Клиент-менеджер":         int(os.environ.get("KM_CHAT_ID",   "0")),
    "Маркетолог":              int(os.environ.get("MKT_CHAT_ID",  "0")),
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# ХРАНИЛИЩЕ СОСТОЯНИЯ 
# ─────────────────────────────────────────────
state = {
    "team_reports": {},
    "od_comment": "",
    "waiting_od_comment": False,
    "iiko_data": [],
    "bitrix_tasks": [],
}


# ─────────────────────────────────────────────
# IIKO — получение KPI через bk152 (iiko Server API)
# ─────────────────────────────────────────────
async def fetch_iiko_kpi() -> list[dict]:
    import hashlib

    try:
        # IIKO_PASSWORD может быть plain или уже SHA1-хеш.
        # Определяем автоматически: SHA1-хеш — это 40 hex-символов.
        pw_raw = IIKO_PASSWORD.strip()
        is_sha1 = len(pw_raw) == 40 and all(c in "0123456789abcdefABCDEF" for c in pw_raw)
        pw_hash = pw_raw.lower() if is_sha1 else hashlib.sha1(pw_raw.encode()).hexdigest()

        today = datetime.now()
        monday = today - timedelta(days=today.weekday())
        date_from = monday.strftime("%Y-%m-%d")
        date_to   = today.strftime("%Y-%m-%d")

        base_url = "https://hachapuri-tetushki-mariko-co.bk152.ru"

        async with httpx.AsyncClient(timeout=30) as client:
            # 1) Авторизация — возвращает токен plain-text'ом
            auth = await client.get(
                f"{base_url}/resto/api/auth",
                params={"login": IIKO_LOGIN, "pass": pw_hash}
            )
            token = auth.text.strip()
            if auth.status_code != 200 or not token or len(token) > 60:
                log.warning(f"iiko auth failed: status={auth.status_code}, body={token[:200]!r}")
                return []

            # 2) OLAP-отчёт по всем точкам сети за неделю
            headers = {"Cookie": f"key={token}"}
            r = await client.post(
                f"{base_url}/resto/api/v2/reports/olap",
                headers=headers,
                json={
                    "reportType": "SALES",
                    "buildSummary": "false",
                    "groupByRowFields": ["Department"],
                    "aggregateFields": ["DishSumInt", "GuestsCount", "OrdersCount"],
                    "filters": {
                        "OpenDate.Typed": {
                            "filterType": "DateRange",
                            "periodType": "CUSTOM",
                            "from": f"{date_from}T00:00:00",
                            "to":   f"{date_to}T23:59:59",
                            "includeLow": True,
                            "includeHigh": True,
                        }
                    }
                }
            )

            # Логаут, чтобы не оставлять открытых сессий
            try:
                await client.get(f"{base_url}/resto/api/logout", headers=headers)
            except Exception:
                pass

            if r.status_code != 200:
                log.warning(f"iiko OLAP failed: status={r.status_code}, body={r.text[:300]!r}")
                return []

            data = r.json()
            rows = data.get("data", [])
            log.info(f"iiko: получено {len(rows)} строк")

            results = []
            for row in rows:
                revenue   = row.get("DishSumInt", 0) or 0
                guests    = row.get("GuestsCount", 0) or 0
                avg_check = round(revenue / guests, 0) if guests else 0
                results.append({
                    "name":      row.get("Department", "—"),
                    "revenue":   f"{int(revenue):,} ₽".replace(",", " "),
                    "guests":    str(int(guests)),
                    "avg_check": f"{int(avg_check)} ₽",
                    "margin":    "—",
                })

            return results

    except Exception as e:
        log.error(f"iiko error: {e}", exc_info=True)
        return []


# ─────────────────────────────────────────────
# BITRIX24 — получение задач ОД за неделю
# ─────────────────────────────────────────────
async def fetch_bitrix_tasks() -> list[dict]:
    try:
        today   = datetime.now()
        monday  = today - timedelta(days=today.weekday())
        date_from = monday.strftime("%Y-%m-%dT00:00:00")

        async with httpx.AsyncClient(timeout=20) as client:
            all_tasks = {}
            for filter_key in ["RESPONSIBLE_ID", "CREATED_BY", "ACCOMPLICE"]:
                r = await client.post(
                    f"{BITRIX_WEBHOOK.rstrip('/')}/tasks.task.list",
                    json={
                        "filter": {
                            filter_key: BITRIX_OD_USER_ID,
                            ">=CREATED_DATE": date_from
                        },
                        "select": ["ID", "TITLE", "STATUS", "DEADLINE"],
                        "order":  {"DEADLINE": "ASC"},
                    }
                )

                if r.status_code != 200:
                    log.warning(f"bitrix {filter_key}: HTTP {r.status_code}, body={r.text[:300]!r}")
                    continue

                data = r.json()
                if "error" in data:
                    log.warning(f"bitrix {filter_key}: {data.get('error')} — {data.get('error_description')}")
                    continue

                tasks = data.get("result", {}).get("tasks", [])
                log.info(f"bitrix {filter_key}: {len(tasks)} задач")
                for t in tasks:
                    all_tasks[t["id"]] = t

            tasks = list(all_tasks.values())
            log.info(f"bitrix: всего уникальных задач {len(tasks)}")

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
                    "status":   STATUS_MAP.get(str(t.get("status", "2")), "В работе"),
                    "deadline": deadline,
                })
            return result

    except Exception as e:
        log.error(f"bitrix error: {e}", exc_info=True)
        return []


# ─────────────────────────────────────────────
# CLAUDE — генерация отчёта
# ─────────────────────────────────────────────
async def generate_report_text() -> tuple[str, str]:
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

    prompt = f"""Ты — операционный директор сети ресторанов «Хачапури Марико». Составляешь еженедельный отчёт для директора по развитию.

ПЕРИОД: {period_str}

===== ДАННЫЕ =====

KPI по точкам (из iiko):
{kpi_lines}

Задачи недели (из Bitrix24):
{task_lines}

Отчёты команды:
{team_lines}

Мой комментарий как ОД:
{comment or 'Не добавлен'}

===== ЗАДАЧА =====

Твоя аудитория — директор по развитию. Ему НЕ нужен пересказ всех данных. Ему нужно быстро понять:
1. Как прошла неделя в цифрах (одна строка)
2. Где проблемы и что мы с ними делаем
3. Есть ли что-то, где нужно его решение или ресурс

===== ПРАВИЛА =====

• Стиль: деловой, конкретный, без канцелярита и общих фраз. Пиши как живой человек, а не как AI-ассистент.
• BLUF (bottom line up front): каждая секция начинается с главного вывода, потом — детали.
• Цифры округляй до тысяч, проценты — до целого. Не пиши «примерно» или «около» — просто число.
• Если данных нет (пустой раздел) — пиши прямо: «Отчёт от [роли] не поступил» или «Данные iiko не пришли». Не выдумывай.
• Проблемные точки называй по имени с конкретной цифрой и причиной, если она известна.
• Никогда не пиши общих напутствий типа «продолжаем работать», «держим фокус», «команда молодцы».

===== ФОРМАТ ВЫВОДА =====

Верни ДВА варианта.

[FULL] — полная версия для архива, разделы:
• Итог недели (2-3 предложения — самое важное)
• KPI по сети (общая выручка, ср. чек, гости; топ-3 и антитоп-3 точки)
• Задачи (что закрыто, что просрочено, что перенесено)
• Проблемные точки (по имени, с причиной и что делаем)
• Отчёты команды (короткий пересказ, не копипаст)
• Запрос к директору (только если реально есть — иначе пропусти раздел)
[/FULL]

[SHORT] — краткая для директора в Telegram, максимум 15 строк:
• Заголовок с периодом
• Главная цифра недели (выручка сети + динамика, если можешь посчитать)
• 2-4 самых важных пункта с эмодзи-маркерами (📈 рост, 📉 падение, 🔴 проблема, ✅ закрыто, ⚠️ внимание)
• Если есть запрос к директору — отдельной строкой «❓ Прошу…»
[/SHORT]"""

    client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    message = await client.messages.create(
        model="claude-sonnet-5",
        max_tokens=3000,
        messages=[{"role": "user", "content": prompt}]
    )

    # Claude возвращает content как список блоков (могут быть thinking + text).
    # Собираем текст только из блоков типа "text".
    text = "".join(
        block.text
        for block in (message.content or [])
        if getattr(block, "type", None) == "text" and getattr(block, "text", None)
    )
    if not text:
        log.error(f"Claude вернул пустой текст. content={message.content!r}")
        return ("Ошибка: Claude вернул пустой ответ", "")

    import re
    full_match  = re.search(r"\[FULL\]([\s\S]*?)\[/FULL\]",  text)
    short_match = re.search(r"\[SHORT\]([\s\S]*?)\[/SHORT\]", text)

    full  = full_match.group(1).strip()  if full_match  else text
    short = short_match.group(1).strip() if short_match else ""
    return full, short


# ─────────────────────────────────────────────
# РАСПИСАНИЕ И ЛОГИКА TELEGRAM
# ─────────────────────────────────────────────
async def job_collect_team(app: Application):
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

    for role, chat_id in TEAM_IDS.items():
        if chat_id != 0 and user_id == chat_id:
            state["team_reports"][role] = text
            await update.message.reply_text(
                f"✅ Принято! Твой отчёт сохранён.\nОН получит всё вместе в конце дня."
            )
            return

    if user_id == OD_CHAT_ID and state["waiting_od_comment"]:
        state["od_comment"] = text
        state["waiting_od_comment"] = False
        await update.message.reply_text("💬 Комментарий сохранён. Генерирую отчёт...")
        await do_generate(ctx.application)
        return

    if user_id == OD_CHAT_ID:
        await update.message.reply_text(
            "Привет! Используй /status чтобы посмотреть статус сбора данных, "
            "или /generate чтобы сгенерировать отчёт вручную."
        )

async def do_generate(app: Application):
    try:
        await app.bot.send_message(OD_CHAT_ID, "⏳ Генерирую отчёт, подожди 10–15 секунд...")
        full, short = await generate_report_text()
        state["_last_report"] = (full, short)

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Отправить директору", callback_data="confirm_send"),
            InlineKeyboardButton("✏️ Добавить правки",    callback_data="edit_report"),
        ]])

        chunks = [full[i:i+4000] for i in range(0, len(full), 4000)]
        for i, chunk in enumerate(chunks):
            if i == len(chunks) - 1:
                await app.bot.send_message(OD_CHAT_ID, chunk, reply_markup=keyboard)
            else:
                await app.bot.send_message(OD_CHAT_ID, chunk)

    except Exception as e:
        log.error(f"generate error: {e}")
        await app.bot.send_message(OD_CHAT_ID, f"❌ Ошибка генерации: {e}")

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

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("status",   cmd_status))
    app.add_handler(CommandHandler("collect",  cmd_collect))
    app.add_handler(CommandHandler("generate", cmd_generate))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT | filters.VOICE, handle_message))

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
