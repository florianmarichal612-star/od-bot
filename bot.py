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
TELEGRAM_TOKEN     = os.environ.get("TELEGRAM_TOKEN", "")
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")

OD_CHAT_ID         = int(os.environ.get("OD_CHAT_ID", "0"))
DIRECTOR_CHAT_ID   = int(os.environ.get("DIRECTOR_CHAT_ID", "0"))

BITRIX_WEBHOOK     = os.environ.get("BITRIX_WEBHOOK", "")
BITRIX_OD_USER_ID  = os.environ.get("BITRIX_OD_USER_ID", "1")

# iiko теперь через n8n-proxy (иначе Railway IP не в whitelist bk152)
IIKO_PROXY_URL     = os.environ.get("IIKO_PROXY_URL", "")


# ─────────────────────────────────────────────
# КОМАНДА: несколько человек в роли
# ─────────────────────────────────────────────
# Переменная окружения TEAM_MEMBERS — строки, разделённые переносом строки \n.
# Формат каждой строки:   Имя Фамилия;Роль;chat_id
# Пустые строки и строки, начинающиеся с #, игнорируются.
#
# Пример:
#   Панфилов Алексей;РШ;111111111
#   Савенков Александр;РШ;222222222
#   Медведева Дарья;Маркетолог;333333333
#   Салищева Алёна;Маркетолог;444444444
#   Добротворская Александра;Маркетолог;555555555
def _parse_team_members(raw: str) -> dict[int, dict]:
    members: dict[int, dict] = {}
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(";")]
        if len(parts) != 3:
            log.warning(f"TEAM_MEMBERS: пропускаю строку (нужно 3 поля через ;): {line!r}")
            continue
        name, role, chat_id_str = parts
        try:
            chat_id = int(chat_id_str)
        except ValueError:
            log.warning(f"TEAM_MEMBERS: пропускаю строку (chat_id не число): {line!r}")
            continue
        if chat_id == 0:
            continue
        members[chat_id] = {"name": name, "role": role}
    return members


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

TEAM_MEMBERS: dict[int, dict] = _parse_team_members(os.environ.get("TEAM_MEMBERS", ""))
log.info(f"TEAM_MEMBERS: загружено {len(TEAM_MEMBERS)} человек")

# ─────────────────────────────────────────────
# ХРАНИЛИЩЕ СОСТОЯНИЯ
# ─────────────────────────────────────────────
# team_reports: { chat_id: {"name": ..., "role": ..., "text": ...} }
state = {
    "team_reports": {},
    "od_comment": "",
    "waiting_od_comment": False,
    "iiko_data": [],
    "bitrix_tasks": [],
}


# ─────────────────────────────────────────────
# IIKO — получение KPI через n8n-proxy
# ─────────────────────────────────────────────
async def fetch_iiko_kpi() -> list[dict]:
    """Идём в n8n-webhook, который сам ходит в iiko со своего IP (в whitelist).
    Возвращает список точек с полями: name, revenue, guests, avg_check, checks, currency."""
    if not IIKO_PROXY_URL:
        log.warning("IIKO_PROXY_URL не задан")
        return []
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.get(IIKO_PROXY_URL)
            if r.status_code != 200:
                log.warning(f"iiko proxy failed: HTTP {r.status_code}, body={r.text[:300]!r}")
                return []
            payload = r.json()
            rows = payload.get("data", [])
            log.info(f"iiko proxy: получено {len(rows)} точек за период {payload.get('period')}")
            for row in rows:
                row.setdefault("margin", "—")
            return rows
    except Exception as e:
        log.error(f"iiko proxy error: {e}", exc_info=True)
        return []


# ─────────────────────────────────────────────
# BITRIX24 — задачи ОД за последние 7 дней
# ─────────────────────────────────────────────
async def fetch_bitrix_tasks() -> list[dict]:
    try:
        today   = datetime.now()
        date_from = (today - timedelta(days=7)).strftime("%Y-%m-%dT00:00:00")

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
    reports = state["team_reports"]
    comment = state["od_comment"]

    period_end   = datetime.now()
    period_start = period_end - timedelta(days=7)
    period_str   = f"{period_start.strftime('%d.%m')} — {period_end.strftime('%d.%m.%Y')}"

    kpi_lines = "\n".join(
        f"  • {p['name']}: выручка {p['revenue']}, гости {p['guests']}, чеков {p.get('checks','—')}, ср.чек {p['avg_check']}"
        for p in iiko
    ) or "  Данные из iiko не получены"

    task_lines = "\n".join(
        f"  • [{t['status']}] {t['title']} (дедлайн: {t['deadline']})"
        for t in tasks
    ) or "  Задачи из Bitrix24 не получены"

    # Отчёты команды: группируем по роли, внутри — по имени
    if reports:
        by_role: dict[str, list[tuple[str, str]]] = {}
        for r in reports.values():
            by_role.setdefault(r["role"], []).append((r["name"], r["text"]))
        team_blocks = []
        for role in sorted(by_role.keys()):
            people = by_role[role]
            people.sort(key=lambda x: x[0])
            people_lines = "\n".join(f"    – {name}: {text}" for name, text in people)
            team_blocks.append(f"  • {role}:\n{people_lines}")
        team_lines = "\n".join(team_blocks)
    else:
        team_lines = "  Отчёты от команды не поступили"

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
• Не строй предположений о прошлых периодах, если данных нет — пиши только про то, что видишь.
• Валюты: точки Астана и Атырау работают в тенге (₸), остальные — в рублях (₽). При расчёте итога по сети НЕ складывай ₸ и ₽. Дай отдельно «Итого Россия» и «Итого Казахстан».
• Проблемные точки называй по имени с конкретной цифрой и причиной, если она известна.
• Никогда не пиши общих напутствий типа «продолжаем работать», «держим фокус», «команда молодцы».
• В отчётах команды несколько человек в одной роли — можешь сослаться на конкретного (например, «РШ Панфилов отметил…»). Если у людей в одной роли разные сигналы — покажи это, а не усредняй.

===== ФОРМАТ ВЫВОДА =====

Верни ДВА варианта.

[FULL] — полная версия для архива, разделы:
• Итог недели (2-3 предложения — самое важное)
• KPI по сети (отдельно РФ и Казахстан: выручка, ср. чек, гости; топ-3 и антитоп-3 точки по выручке в своей валюте)
• Задачи (что закрыто, что просрочено, что перенесено)
• Проблемные точки (по имени, с причиной и что делаем)
• Отчёты команды (короткий пересказ, не копипаст)
• Запрос к директору (только если реально есть — иначе пропусти раздел)
[/FULL]

[SHORT] — краткая для директора в Telegram, максимум 15 строк:
• Заголовок с периодом
• Главная цифра недели (выручка сети РФ + отдельно Казахстан)
• 2-4 самых важных пункта с эмодзи-маркерами (📈 рост, 📉 падение, 🔴 проблема, ✅ закрыто, ⚠️ внимание)
• Если есть запрос к директору — отдельной строкой «❓ Прошу…»
[/SHORT]"""

    client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    message = await client.messages.create(
        model="claude-sonnet-5",
        max_tokens=16000,
        messages=[{"role": "user", "content": prompt}]
    )

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
    if not TEAM_MEMBERS:
        log.warning("TEAM_MEMBERS пуст — рассылка команде пропущена")
        return
    for chat_id, m in TEAM_MEMBERS.items():
        name = m["name"]
        role = m["role"]
        first_name = name.split()[0] if name else ""
        try:
            await app.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"👋 Привет, {first_name}! Пятница — время короткого отчёта.\n\n"
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
            log.error(f"Не удалось написать {role} — {name} (chat_id={chat_id}): {e}")

async def job_collect_kpi_and_notify_od(app: Application):
    log.info("Сбор данных из iiko и Bitrix24...")
    state["iiko_data"]    = await fetch_iiko_kpi()
    state["bitrix_tasks"] = await fetch_bitrix_tasks()

    # Кто не прислал отчёт
    missing = [
        f"{m['role']} — {m['name']}"
        for chat_id, m in TEAM_MEMBERS.items()
        if chat_id not in state["team_reports"]
    ]
    missing_note = ""
    if missing:
        missing_note = "\n\n⚠️ Не прислали отчёт:\n  • " + "\n  • ".join(missing)

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
            f"Отчёты команды: {len(state['team_reports'])}/{len(TEAM_MEMBERS)}\n\n"
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

    # Отчёт от члена команды
    if user_id in TEAM_MEMBERS:
        m = TEAM_MEMBERS[user_id]
        state["team_reports"][user_id] = {
            "name": m["name"],
            "role": m["role"],
            "text": text,
        }
        first_name = m["name"].split()[0] if m["name"] else ""
        await update.message.reply_text(
            f"✅ Принято, {first_name}! Твой отчёт сохранён.\nОД получит всё вместе в конце дня."
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
    # Определяем — свой это или чужой
    user_id = update.effective_chat.id
    if user_id in TEAM_MEMBERS:
        m = TEAM_MEMBERS[user_id]
        first = m["name"].split()[0] if m["name"] else ""
        await update.message.reply_text(
            f"👋 Привет, {first}! Ты подключён(а) как *{m['role']}*.\n\n"
            f"Каждую пятницу в 09:00 я пришлю форму — ответишь одним сообщением "
            f"(можно голосовым), и всё уйдёт ОД в общий еженедельный отчёт.",
            parse_mode="Markdown"
        )
        return

    if user_id == OD_CHAT_ID:
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
            "/collect — вручную запустить сбор из iiko и Bitrix24\n"
            "/myid — показать твой chat_id",
            parse_mode="Markdown"
        )
        return

    # Незнакомец — отдаём chat_id, чтобы ОД мог его добавить
    await update.message.reply_text(
        f"👋 Привет! Ты пока не подключён(а) к боту.\n\n"
        f"Твой chat_id: `{user_id}`\n\n"
        f"Перешли его Веронике, чтобы она добавила тебя в команду.",
        parse_mode="Markdown"
    )

async def cmd_myid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Простая команда, чтобы любой мог узнать свой chat_id — удобно для подключения."""
    user_id = update.effective_chat.id
    await update.message.reply_text(
        f"Твой chat_id: `{user_id}`",
        parse_mode="Markdown"
    )

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    reports     = state["team_reports"]
    iiko_count  = len(state["iiko_data"])
    tasks_count = len(state["bitrix_tasks"])

    text = (
        f"📊 *Статус сбора данных*\n\n"
        f"iiko (точки): {iiko_count} {'✅' if iiko_count else '❌ не загружено'}\n"
        f"Bitrix24 (задачи): {tasks_count} {'✅' if tasks_count else '❌ не загружено'}\n\n"
        f"Команда ({len(reports)}/{len(TEAM_MEMBERS)}):\n"
    )

    if not TEAM_MEMBERS:
        text += "  • TEAM_MEMBERS не задан\n"
    else:
        # Группируем по роли для читаемости
        by_role: dict[str, list[tuple[int, str]]] = {}
        for chat_id, m in TEAM_MEMBERS.items():
            by_role.setdefault(m["role"], []).append((chat_id, m["name"]))
        for role in sorted(by_role.keys()):
            text += f"  *{role}:*\n"
            for chat_id, name in sorted(by_role[role], key=lambda x: x[1]):
                mark = "✅" if chat_id in reports else "⏳"
                text += f"    {mark} {name}\n"

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
    app.add_handler(CommandHandler("myid",     cmd_myid))
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
