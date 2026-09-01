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
# Формат каждой строки:   Имя Фамилия;Роль;chat_id;bitrix_id
# Четвёртое поле (bitrix_id) — опциональное: если задано, бот подтянет задачи
# этого человека из Битрикса и добавит в отчёт рядом с его самоотчётом,
# чтобы можно было сравнить самоотчёт с фактами Б24.
# Пустые строки и строки, начинающиеся с #, игнорируются.
#
# Пример (chat_id получить: попроси каждого написать боту /myid):
#   Панфилов Алексей;РШ;111111111;1865
#   Савенков Александр;РШ;222222222;2393
#   Медведева Дарья;Маркетолог;333333333;2211
#   Салищева Алёна;Маркетолог;444444444;2229
#   Добротворская Александра;Маркетолог;555555555;2367
#   Проненко Эва;Клиент-менеджер;666666666;1601
#   Данилина Юлия;Клиент-менеджер;777777777;1853
def _parse_team_members(raw: str) -> dict[int, dict]:
    members: dict[int, dict] = {}
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(";")]
        if len(parts) not in (3, 4):
            log.warning(f"TEAM_MEMBERS: пропускаю строку (нужно 3 или 4 поля через ;): {line!r}")
            continue
        name = parts[0]
        role = parts[1]
        chat_id_str = parts[2]
        bitrix_id = parts[3] if len(parts) == 4 and parts[3] else ""
        try:
            chat_id = int(chat_id_str)
        except ValueError:
            log.warning(f"TEAM_MEMBERS: пропускаю строку (chat_id не число): {line!r}")
            continue
        if chat_id == 0:
            continue
        members[chat_id] = {"name": name, "role": role, "bitrix_id": bitrix_id}
    return members


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

TEAM_MEMBERS: dict[int, dict] = _parse_team_members(os.environ.get("TEAM_MEMBERS", ""))
_with_bitrix = sum(1 for m in TEAM_MEMBERS.values() if m.get("bitrix_id"))
log.info(f"TEAM_MEMBERS: загружено {len(TEAM_MEMBERS)} человек ({_with_bitrix} с bitrix_id)")


# ─────────────────────────────────────────────
# WHITELIST — кто вообще может пользоваться ботом
# ─────────────────────────────────────────────
def is_authorized(user_id: int) -> bool:
    """Разрешён ли пользователь.

    ОД, директор по развитию и все участники TEAM_MEMBERS — да.
    Все остальные — нет: бот молча их игнорирует.
    Команда /myid — единственное публичное исключение (см. cmd_myid),
    нужна для подключения новых людей.
    """
    if user_id == OD_CHAT_ID or user_id == DIRECTOR_CHAT_ID:
        return True
    return user_id in TEAM_MEMBERS


# ─────────────────────────────────────────────
# ХРАНИЛИЩЕ СОСТОЯНИЯ
# ─────────────────────────────────────────────
# team_reports:       { chat_id: {"name": ..., "role": ..., "text": ...} }
# team_bitrix_tasks:  { chat_id: [task, task, ...] }  — задачи каждого члена команды из Б24
state = {
    "team_reports": {},
    "od_comment": "",
    "waiting_od_comment": False,
    "iiko_data": [],
    "bitrix_tasks": [],       # задачи Вероники (ОД)
    "team_bitrix_tasks": {},  # задачи каждого члена команды
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
# BITRIX24 — задачи за последние 7 дней
# ─────────────────────────────────────────────
# Правильный маппинг статусов Bitrix24 REST (tasks.task.list):
#   1 — Новая
#   2 — Ждёт выполнения
#   3 — Выполняется
#   4 — Ожидает контроля (постановщика)
#   5 — Завершена
#   6 — Отложена
#   7 — Отклонена
# subStatus = "-3" означает ПРОСРОЧЕНА (перекрывает базовый статус)
BITRIX_STATUS_MAP = {
    "1": ("🆕", "Новая"),
    "2": ("⏳", "Ждёт выполнения"),
    "3": ("⏳", "Выполняется"),
    "4": ("⚠️", "Ожидает контроля"),
    "5": ("✅", "Завершена"),
    "6": ("➡️", "Отложена"),
    "7": ("🚫", "Отклонена"),
}


def _format_task_status(task_raw: dict) -> tuple[str, str, int]:
    """Читаемый статус задачи. Возвращает (иконка, полный текст статуса, приоритет для сортировки).
    Приоритет: 0 — просрочена, 1 — ожидает контроля, 2 — активная (ждёт/выполняется),
    3 — отложена, 4 — завершена, 5 — отклонена. Меньше = важнее в отчёте."""
    status = str(task_raw.get("status", "2"))
    sub_status = str(task_raw.get("subStatus", status))
    label_from_api = task_raw.get("statusLabel", "")

    # Просрочка перекрывает базовый статус
    if sub_status == "-3":
        return ("❌", "❌ Просрочена", 0)

    emoji, label = BITRIX_STATUS_MAP.get(status, ("⏳", label_from_api or f"статус {status}"))
    priority_map = {
        "4": 1,  # ожидает контроля
        "1": 2, "2": 2, "3": 2,  # активные
        "6": 3,  # отложена
        "5": 4,  # завершена
        "7": 5,  # отклонена
    }
    priority = priority_map.get(status, 2)
    return (emoji, f"{emoji} {label}", priority)


async def _fetch_tasks_for_user(client: httpx.AsyncClient, user_id: str, date_from: str) -> list[dict]:
    """Задачи одного пользователя Битрикса за период. Дедуплицируем по ID.
    Смотрим три роли: исполнитель, автор, соисполнитель."""
    if not user_id or not str(user_id).strip():
        return []
    all_tasks: dict[str, dict] = {}
    for filter_key in ["RESPONSIBLE_ID", "CREATED_BY", "ACCOMPLICE"]:
        try:
            r = await client.post(
                f"{BITRIX_WEBHOOK.rstrip('/')}/tasks.task.list",
                json={
                    "filter": {
                        filter_key: user_id,
                        ">=CREATED_DATE": date_from
                    },
                    # SUB_STATUS нужен, чтобы отловить просрочку (-3)
                    "select": ["ID", "TITLE", "STATUS", "SUB_STATUS", "DEADLINE"],
                    "order":  {"DEADLINE": "ASC"},
                }
            )
            if r.status_code != 200:
                log.warning(f"bitrix user={user_id} {filter_key}: HTTP {r.status_code}, body={r.text[:300]!r}")
                continue
            data = r.json()
            if "error" in data:
                log.warning(f"bitrix user={user_id} {filter_key}: {data.get('error')} — {data.get('error_description')}")
                continue
            tasks = data.get("result", {}).get("tasks", [])
            for t in tasks:
                all_tasks[t["id"]] = t
        except Exception as e:
            log.error(f"bitrix user={user_id} {filter_key} error: {e}", exc_info=True)

    result = []
    for t in all_tasks.values():
        deadline = t.get("deadline", "") or ""
        if deadline:
            try:
                deadline = datetime.fromisoformat(deadline).strftime("%d.%m")
            except Exception:
                pass
        emoji, status_text, priority = _format_task_status(t)
        result.append({
            "title":    t.get("title", ""),
            "status":   status_text,
            "priority": priority,
            "deadline": deadline,
        })
    log.info(f"bitrix user={user_id}: {len(result)} уникальных задач")
    return result


async def fetch_bitrix_tasks() -> list[dict]:
    """Задачи ОД (Вероники) за последние 7 дней."""
    if not BITRIX_WEBHOOK:
        return []
    date_from = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%dT00:00:00")
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            return await _fetch_tasks_for_user(client, BITRIX_OD_USER_ID, date_from)
    except Exception as e:
        log.error(f"bitrix (ОД) error: {e}", exc_info=True)
        return []


async def fetch_team_bitrix_tasks() -> dict[int, list[dict]]:
    """Задачи каждого члена команды с указанным bitrix_id за последние 7 дней.
    Возвращает { chat_id: [tasks] } — включает только тех, у кого есть bitrix_id."""
    if not BITRIX_WEBHOOK:
        return {}
    date_from = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%dT00:00:00")
    result: dict[int, list[dict]] = {}
    targets = [(cid, m) for cid, m in TEAM_MEMBERS.items() if m.get("bitrix_id")]
    if not targets:
        return {}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            # Параллельно опрашиваем всех — быстрее, чем по очереди
            coros = [_fetch_tasks_for_user(client, m["bitrix_id"], date_from) for _, m in targets]
            results = await asyncio.gather(*coros, return_exceptions=True)
            for (chat_id, m), res in zip(targets, results):
                if isinstance(res, Exception):
                    log.error(f"team bitrix fetch failed for {m['name']}: {res}")
                    result[chat_id] = []
                else:
                    result[chat_id] = res
    except Exception as e:
        log.error(f"team bitrix error: {e}", exc_info=True)
    return result


# ─────────────────────────────────────────────
# CLAUDE — генерация отчёта
# ─────────────────────────────────────────────
def _format_tasks_short(tasks: list[dict], limit: int = 15) -> str:
    """Форматирует задачи одного человека в компактный список для промпта.
    Сортировка по приоритету: сначала просроченные, потом активные, потом завершённые."""
    if not tasks:
        return "нет задач за неделю"
    ordered = sorted(tasks, key=lambda t: t.get("priority", 9))[:limit]
    lines = [
        f"        - [{t['status']}] {t['title']}" + (f" (дедлайн {t['deadline']})" if t['deadline'] else "")
        for t in ordered
    ]
    tail = f"\n        … и ещё {len(tasks) - limit} задач" if len(tasks) > limit else ""
    return "\n" + "\n".join(lines) + tail


async def generate_report_text() -> tuple[str, str]:
    iiko  = state["iiko_data"]
    od_tasks = state["bitrix_tasks"]
    reports = state["team_reports"]
    team_tasks = state["team_bitrix_tasks"]
    comment = state["od_comment"]

    period_end   = datetime.now()
    period_start = period_end - timedelta(days=7)
    period_str   = f"{period_start.strftime('%d.%m')} — {period_end.strftime('%d.%m.%Y')}"

    kpi_lines = "\n".join(
        f"  • {p['name']}: выручка {p['revenue']}, гости {p['guests']}, чеков {p.get('checks','—')}, ср.чек {p['avg_check']}"
        for p in iiko
    ) or "  Данные из iiko не получены"

    od_task_lines = "\n".join(
        f"  • [{t['status']}] {t['title']} (дедлайн: {t['deadline']})"
        for t in sorted(od_tasks, key=lambda t: t.get("priority", 9))
    ) or "  Задачи из Bitrix24 не получены"

    # Отчёты команды: у каждого человека — его самоотчёт + его задачи из Б24 рядом.
    # Показываем всех членов команды, даже тех, кто не прислал самоотчёт —
    # это позволяет Клоду отметить отсутствие + при этом увидеть, что человек делал по Б24.
    if TEAM_MEMBERS:
        by_role: dict[str, list[tuple[int, dict]]] = {}
        for chat_id, m in TEAM_MEMBERS.items():
            by_role.setdefault(m["role"], []).append((chat_id, m))
        team_blocks = []
        for role in sorted(by_role.keys()):
            people = sorted(by_role[role], key=lambda x: x[1]["name"])
            people_lines = []
            for chat_id, m in people:
                name = m["name"]
                self_report = reports.get(chat_id, {}).get("text") or "самоотчёт не прислан"
                his_tasks = team_tasks.get(chat_id, [])
                if m.get("bitrix_id"):
                    tasks_str = _format_tasks_short(his_tasks)
                else:
                    tasks_str = "bitrix_id не задан — задачи не сверялись"
                people_lines.append(
                    f"    – {name}:\n"
                    f"      • самоотчёт: {self_report}\n"
                    f"      • задачи из Б24: {tasks_str}"
                )
            team_blocks.append(f"  • {role}:\n" + "\n".join(people_lines))
        team_lines = "\n".join(team_blocks)
    else:
        team_lines = "  Команда (TEAM_MEMBERS) не настроена"

    prompt = f"""Ты — операционный директор сети ресторанов «Хачапури Марико». Составляешь еженедельный отчёт для директора по развитию.

ПЕРИОД: {period_str}

===== ДАННЫЕ =====

KPI по точкам (из iiko):
{kpi_lines}

Мои задачи недели (из Bitrix24, я — ОД):
{od_task_lines}

Отчёты команды (самоотчёт + задачи каждого из Б24 за тот же период):
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

===== СВЕРКА САМООТЧЁТ vs Б24 =====

У каждого члена команды рядом с самоотчётом показаны его задачи из Битрикса за тот же период. Сравни:
• Если человек в самоотчёте пишет «закрыл X», а в Б24 задача X всё ещё в активном статусе или «❌ Просрочена» — это расхождение, отметь его конкретно («Панфилов сообщил о закрытии, по Б24 не подтверждено»).
• Если по Б24 есть просроченные задачи, а в самоотчёте про них тишина — тоже сигнал.
• Если самоотчёта нет, но по Б24 видно активную работу — упомяни, что делал по фактам Б24.
• Если самоотчёта нет и в Б24 активности нет — так и напиши: «неделя прошла без сигналов».
• Не устраивай прокурорский тон — это рабочая сверка, а не разбор полётов.

===== ФОРМАТ ВЫВОДА =====

Верни ДВА варианта.

[FULL] — полная версия для архива, разделы:
• Итог недели (2-3 предложения — самое важное)
• KPI по сети (отдельно РФ и Казахстан: выручка, ср. чек, гости; топ-3 и антитоп-3 точки по выручке в своей валюте)
• Мои задачи как ОД (что закрыто, что просрочено, что перенесено)
• Команда (по каждому: коротко чем занимался, есть ли расхождения самоотчёт vs Б24)
• Проблемные точки (по имени, с причиной и что делаем)
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
    log.info("Сбор данных из iiko и Bitrix24 (ОД + команда)...")
    # Параллельно — быстрее
    iiko_task = asyncio.create_task(fetch_iiko_kpi())
    od_tasks_task = asyncio.create_task(fetch_bitrix_tasks())
    team_tasks_task = asyncio.create_task(fetch_team_bitrix_tasks())
    state["iiko_data"]         = await iiko_task
    state["bitrix_tasks"]      = await od_tasks_task
    state["team_bitrix_tasks"] = await team_tasks_task

    # Кто не прислал отчёт
    missing = [
        f"{m['role']} — {m['name']}"
        for chat_id, m in TEAM_MEMBERS.items()
        if chat_id not in state["team_reports"]
    ]
    missing_note = ""
    if missing:
        missing_note = "\n\n⚠️ Не прислали отчёт:\n  • " + "\n  • ".join(missing)

    total_team_tasks = sum(len(v) for v in state["team_bitrix_tasks"].values())

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✍️ Добавить комментарий", callback_data="add_comment"),
        InlineKeyboardButton("⏭ Пропустить", callback_data="skip_comment"),
    ]])

    await app.bot.send_message(
        chat_id=OD_CHAT_ID,
        text=(
            f"📋 *Данные за неделю собраны.*{missing_note}\n\n"
            f"Из iiko: {len(state['iiko_data'])} точек\n"
            f"Задачи ОД из Bitrix24: {len(state['bitrix_tasks'])}\n"
            f"Задачи команды из Bitrix24: {total_team_tasks} (по {len(state['team_bitrix_tasks'])} чел.)\n"
            f"Отчёты команды: {len(state['team_reports'])}/{len(TEAM_MEMBERS)}\n\n"
            f"Добавь итоговый комментарий (голосом или текстом) — или пропусти."
        ),
        parse_mode="Markdown",
        reply_markup=keyboard
    )
    state["waiting_od_comment"] = False

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    # Кнопки под сообщениями бота — только для авторизованных
    user_id = query.from_user.id if query.from_user else 0
    if not is_authorized(user_id):
        await query.answer("Нет доступа", show_alert=False)
        return
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

    # Незнакомец — молча игнорируем (в лог для отладки, без ответа)
    if not is_authorized(user_id):
        log.info(f"Отклонено сообщение от неавторизованного chat_id={user_id}: {text[:80]!r}")
        return

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
            "• 16:00 — собираю KPI из iiko и задачи из Bitrix24 (свои + команды)\n"
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

    if user_id == DIRECTOR_CHAT_ID:
        await update.message.reply_text(
            "👋 Это бот еженедельного отчёта ОД сети «Хачапури Марико». "
            "По пятницам сюда придёт короткая сводка недели."
        )
        return

    # Всё остальное — приватный бот, нейтральный ответ
    await update.message.reply_text(
        "Этот бот приватный — доступ только у команды сети «Хачапури Марико»."
    )

async def cmd_myid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Единственная публичная команда — чтобы новый человек мог узнать свой chat_id
    и передать его ОД для подключения. Не выдаёт никаких данных бота."""
    user_id = update.effective_chat.id
    await update.message.reply_text(
        f"Твой chat_id: `{user_id}`\n\n"
        f"Перешли его Веронике, чтобы она подключила тебя к боту.",
        parse_mode="Markdown"
    )

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != OD_CHAT_ID:
        return
    reports     = state["team_reports"]
    team_tasks  = state["team_bitrix_tasks"]
    iiko_count  = len(state["iiko_data"])
    od_tasks_ct = len(state["bitrix_tasks"])

    text = (
        f"📊 *Статус сбора данных*\n\n"
        f"iiko (точки): {iiko_count} {'✅' if iiko_count else '❌ не загружено'}\n"
        f"Задачи ОД (Б24): {od_tasks_ct} {'✅' if od_tasks_ct else '⏳ не загружено'}\n\n"
        f"Команда ({len(reports)}/{len(TEAM_MEMBERS)}):\n"
    )

    if not TEAM_MEMBERS:
        text += "  • TEAM_MEMBERS не задан\n"
    else:
        # Группируем по роли для читаемости
        by_role: dict[str, list[tuple[int, dict]]] = {}
        for chat_id, m in TEAM_MEMBERS.items():
            by_role.setdefault(m["role"], []).append((chat_id, m))
        for role in sorted(by_role.keys()):
            text += f"  *{role}:*\n"
            for chat_id, m in sorted(by_role[role], key=lambda x: x[1]["name"]):
                report_mark = "✅" if chat_id in reports else "⏳"
                if m.get("bitrix_id"):
                    n = len(team_tasks.get(chat_id, []))
                    b24 = f"Б24: {n}"
                else:
                    b24 = "Б24: —"
                text += f"    {report_mark} {m['name']}  ({b24})\n"

    if state["od_comment"]:
        text += f"\nКомментарий ОД: ✅ добавлен"
    else:
        text += f"\nКомментарий ОД: ⏳ не добавлен"

    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_collect(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != OD_CHAT_ID:
        return
    await update.message.reply_text("🔄 Собираю данные из iiko и Bitrix24 (свои + команды)...")
    iiko_task = asyncio.create_task(fetch_iiko_kpi())
    od_tasks_task = asyncio.create_task(fetch_bitrix_tasks())
    team_tasks_task = asyncio.create_task(fetch_team_bitrix_tasks())
    state["iiko_data"]         = await iiko_task
    state["bitrix_tasks"]      = await od_tasks_task
    state["team_bitrix_tasks"] = await team_tasks_task
    total_team_tasks = sum(len(v) for v in state["team_bitrix_tasks"].values())
    await update.message.reply_text(
        f"✅ Готово:\n"
        f"• iiko: {len(state['iiko_data'])} точек\n"
        f"• Задачи ОД: {len(state['bitrix_tasks'])}\n"
        f"• Задачи команды: {total_team_tasks} (по {len(state['team_bitrix_tasks'])} чел.)"
    )

async def cmd_generate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != OD_CHAT_ID:
        return
    if not state["iiko_data"] and not state["bitrix_tasks"] and not state["team_bitrix_tasks"]:
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
