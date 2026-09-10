"""
Веб-часть Mini App «План / Факт» — отдаёт webapp/dashboard.html и
проксирует данные из n8n-вебхука (который читает лист «Отчёт» в
Google Sheets, куда раз в день пишет workflow «Ежедневная корреляция
план/факт РФК»).
"""
from __future__ import annotations

import asyncio
import logging
import os

import httpx
from aiohttp import web

from checklist_auth import verify_init_data
from checklist_config import OD_CHAT_ID

log = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
PLAN_FACT_PROXY_URL = os.environ.get(
    "PLAN_FACT_PROXY_URL", "https://flow.rfk.su/webhook/od-bot-plan-fact"
)
# Живой запрос к iiko по всем 4 брендам (для среднего чека и кол-ва чеков) —
# в отличие от план/факта, тут нет дневного кеша, считается при каждом
# открытии дашборда. Из-за живого iiko-запроса может занимать до ~40 сек.
CHECKS_PROXY_URL = os.environ.get(
    "CHECKS_PROXY_URL", "https://flow.rfk.su/webhook/od-bot-checks-mtd"
)
DEV_MODE = os.environ.get("CHECKLIST_DEV_MODE", "") == "1"

HERE = os.path.dirname(os.path.abspath(__file__))
HTML_PATH = os.path.join(HERE, "webapp", "dashboard.html")
LOGO_PATH = os.path.join(HERE, "webapp", "logo.svg")


def _parse_pairs(raw: str) -> dict[int, str]:
    result: dict[int, str] = {}
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(";")]
        if len(parts) < 2:
            continue
        try:
            result[int(parts[1])] = parts[0]
        except ValueError:
            continue
    return result


# Отдельный список от чек-листа: это финансовые данные по всей сети
# (4 бренда), а не по одной точке — доступ выдаём осознанно, отдельно.
DASHBOARD_USERS = _parse_pairs(os.environ.get("DASHBOARD_USERS", ""))


def _authorized_user(request: web.Request) -> tuple[int, str] | None:
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    if DEV_MODE and not init_data:
        return (OD_CHAT_ID or 1, "Dev ОД")
    parsed = verify_init_data(init_data, BOT_TOKEN)
    if not parsed:
        return None
    user = parsed.get("user") or {}
    chat_id = user.get("id")
    if not chat_id:
        return None
    if chat_id == OD_CHAT_ID:
        return (chat_id, user.get("first_name") or "ОД")
    if chat_id in DASHBOARD_USERS:
        return (chat_id, DASHBOARD_USERS[chat_id])
    return None


async def handle_index(request: web.Request) -> web.Response:
    with open(HTML_PATH, encoding="utf-8") as f:
        return web.Response(
            text=f.read(),
            content_type="text/html",
            headers={"Cache-Control": "no-store, must-revalidate"},
        )


async def handle_logo(request: web.Request) -> web.Response:
    with open(LOGO_PATH, encoding="utf-8") as f:
        return web.Response(
            text=f.read(),
            content_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=86400"},
        )


async def _fetch_json(client: httpx.AsyncClient, url: str) -> dict | None:
    try:
        r = await client.get(url)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error(f"proxy fetch failed ({url}): {e}", exc_info=True)
        return None


async def handle_data(request: web.Request) -> web.Response:
    auth = _authorized_user(request)
    if not auth:
        return web.json_response({"error": "unauthorized"}, status=403)
    _, name = auth

    async with httpx.AsyncClient(timeout=45) as client:
        plan_fact, checks = await asyncio.gather(
            _fetch_json(client, PLAN_FACT_PROXY_URL),
            _fetch_json(client, CHECKS_PROXY_URL),
        )

    if plan_fact is None:
        return web.json_response({"error": "upstream_failed"}, status=502)

    checks_by_key: dict[tuple[str, str], dict] = {}
    if checks:
        for row in checks.get("rows", []):
            key = (row.get("brand", ""), row.get("point", ""))
            checks_by_key[key] = row

    for row in plan_fact.get("rows", []):
        key = (row.get("Бренд", ""), row.get("Точка", ""))
        match = checks_by_key.get(key)
        if match:
            row["Чеки"] = match.get("checks")
            row["СрЧек"] = match.get("avg_check")
            row["ВыручкаФакт"] = match.get("revenue_mtd")
            row["ПланЧеков"] = match.get("plan_checks")
            row["ПрогнозЧеков"] = match.get("forecast_checks")
            row["ПланСрЧек"] = match.get("plan_avg_check")
            row["ПрошлыйГод"] = match.get("last_year_total")
            row["ДинамикаГод"] = match.get("yoy_pct")
            row["ПрошлыйГодЧеков"] = match.get("last_year_checks")
            row["ДинамикаГодЧеков"] = match.get("yoy_checks_pct")

    plan_fact["name"] = name
    plan_fact["checks_available"] = checks is not None
    if checks and checks.get("data_date"):
        plan_fact["data_date"] = checks["data_date"]
    return web.json_response(plan_fact)


def register_dashboard_routes(app: web.Application):
    app.router.add_get("/dashboard", handle_index)
    app.router.add_get("/dashboard/", handle_index)
    app.router.add_get("/api/dashboard/data", handle_data)
    app.router.add_get("/logo.svg", handle_logo)
