"""
Веб-сервер (aiohttp) для Mini App «Чек-лист визита ОД».
Отдаёт статическую страницу webapp/checklist.html и REST API для
черновиков, истории визитов и создания задач в Bitrix24.
"""
from __future__ import annotations

import logging
import os

import httpx
from aiohttp import web

import checklist_db as db
from checklist_auth import verify_init_data
from checklist_config import CHECKLIST_USERS, OD_CHAT_ID, checklist_user_name

log = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
BITRIX_WEBHOOK = os.environ.get("BITRIX_WEBHOOK", "")
BITRIX_DEFAULT_RESPONSIBLE = os.environ.get("BITRIX_OD_USER_ID", "1")
DEV_MODE = os.environ.get("CHECKLIST_DEV_MODE", "") == "1"

HERE = os.path.dirname(os.path.abspath(__file__))
HTML_PATH = os.path.join(HERE, "webapp", "checklist.html")


def _authorized_user(request: web.Request) -> tuple[int, str] | None:
    """(chat_id, имя) авторизованного пользователя чек-листа, либо None."""
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    if DEV_MODE and not init_data:
        return (OD_CHAT_ID or 1, "Dev ОД")
    parsed = verify_init_data(init_data, BOT_TOKEN)
    if not parsed:
        # ВРЕМЕННО: диагностика проблем с доступом к чек-листу.
        log.warning(
            f"checklist auth FAILED: init_data_len={len(init_data)} "
            f"bot_token_set={bool(BOT_TOKEN)} od_chat_id={OD_CHAT_ID}"
        )
        return None
    user = parsed.get("user") or {}
    chat_id = user.get("id")
    if not chat_id:
        log.warning(f"checklist auth: verified but no user.id in parsed data: {parsed!r}")
        return None
    log.info(
        f"checklist auth OK: chat_id={chat_id} od_chat_id={OD_CHAT_ID} "
        f"in_checklist_users={chat_id in CHECKLIST_USERS}"
    )
    if chat_id == OD_CHAT_ID:
        return (chat_id, user.get("first_name") or "ОД")
    if chat_id in CHECKLIST_USERS:
        return (chat_id, checklist_user_name(chat_id))
    return None


async def handle_index(request: web.Request) -> web.Response:
    with open(HTML_PATH, encoding="utf-8") as f:
        return web.Response(
            text=f.read(),
            content_type="text/html",
            headers={"Cache-Control": "no-store, must-revalidate"},
        )


async def handle_get_draft(request: web.Request) -> web.Response:
    auth = _authorized_user(request)
    if not auth:
        return web.json_response({"error": "unauthorized"}, status=403)
    chat_id, name = auth
    return web.json_response({"draft": db.get_draft(chat_id), "name": name})


async def handle_save_draft(request: web.Request) -> web.Response:
    auth = _authorized_user(request)
    if not auth:
        return web.json_response({"error": "unauthorized"}, status=403)
    chat_id, _ = auth
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "bad_json"}, status=400)
    db.save_draft(chat_id, body.get("state", {}))
    return web.json_response({"ok": True})


async def handle_submit(request: web.Request) -> web.Response:
    auth = _authorized_user(request)
    if not auth:
        return web.json_response({"error": "unauthorized"}, status=403)
    chat_id, name = auth
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "bad_json"}, status=400)
    state = body.get("state", {})
    meta = state.get("meta", {})
    visit_id = db.save_visit(chat_id, name, meta.get("city", ""), meta.get("dates", ""), state)
    return web.json_response({"ok": True, "visit_id": visit_id})


async def handle_history(request: web.Request) -> web.Response:
    if not _authorized_user(request):
        return web.json_response({"error": "unauthorized"}, status=403)
    return web.json_response({"visits": db.list_visits()})


async def handle_visit(request: web.Request) -> web.Response:
    if not _authorized_user(request):
        return web.json_response({"error": "unauthorized"}, status=403)
    try:
        visit_id = int(request.match_info["visit_id"])
    except ValueError:
        return web.json_response({"error": "bad_id"}, status=400)
    visit = db.get_visit(visit_id)
    if not visit:
        return web.json_response({"error": "not_found"}, status=404)
    return web.json_response(visit)


async def _bitrix_create_task(client: httpx.AsyncClient, title: str, description: str) -> dict:
    try:
        r = await client.post(
            f"{BITRIX_WEBHOOK.rstrip('/')}/tasks.task.add",
            json={"fields": {
                "TITLE": title[:250],
                "DESCRIPTION": description,
                "RESPONSIBLE_ID": BITRIX_DEFAULT_RESPONSIBLE,
            }},
        )
        data = r.json()
    except Exception as e:
        log.error(f"bitrix task create error: {e}", exc_info=True)
        return {"ok": False, "error": str(e)}
    if "error" in data:
        return {"ok": False, "error": data.get("error_description", data.get("error"))}
    task_id = (data.get("result") or {}).get("task", {}).get("id")
    return {"ok": True, "id": task_id}


async def handle_bitrix_tasks(request: web.Request) -> web.Response:
    if not _authorized_user(request):
        return web.json_response({"error": "unauthorized"}, status=403)
    if not BITRIX_WEBHOOK:
        return web.json_response({"error": "bitrix_not_configured"}, status=400)
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "bad_json"}, status=400)
    tasks = body.get("tasks", [])
    if not tasks:
        return web.json_response({"error": "no_tasks"}, status=400)

    results = []
    async with httpx.AsyncClient(timeout=20) as client:
        for t in tasks:
            title = f"[{t.get('section', '')}] {t.get('question', '')}"
            description = (
                f"{t.get('comment') or '(без комментария)'}\n\n"
                f"Ответственный (по чек-листу): {t.get('responsible', '')}\n"
                f"Приоритет: {t.get('priority', '')}\n"
                f"Объект: {t.get('object', '')}"
            )
            results.append(await _bitrix_create_task(client, title, description))

    created = sum(1 for r in results if r.get("ok"))
    return web.json_response({"ok": True, "created": created, "total": len(tasks), "results": results})


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


def build_web_app() -> web.Application:
    db.init_db()
    app = web.Application()
    app.router.add_get("/checklist", handle_index)
    app.router.add_get("/checklist/", handle_index)
    app.router.add_get("/api/checklist/draft", handle_get_draft)
    app.router.add_post("/api/checklist/draft", handle_save_draft)
    app.router.add_post("/api/checklist/submit", handle_submit)
    app.router.add_get("/api/checklist/history", handle_history)
    app.router.add_get("/api/checklist/visit/{visit_id}", handle_visit)
    app.router.add_post("/api/checklist/bitrix-tasks", handle_bitrix_tasks)
    app.router.add_get("/health", handle_health)

    from dashboard_web import register_dashboard_routes
    register_dashboard_routes(app)

    return app


async def start_web_server(port: int) -> web.AppRunner:
    app = build_web_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    return runner
