"""
Общие настройки доступа для фичи «Чек-лист визита ОД» (Mini App).
Список разрешённых ОД задаётся вручную через переменную окружения
OD_CHECKLIST_USERS — по одной строке на человека: Имя Фамилия;chat_id
"""
from __future__ import annotations

import os


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


OD_CHAT_ID = int(os.environ.get("OD_CHAT_ID", "0") or "0")
CHECKLIST_USERS: dict[int, str] = _parse_pairs(os.environ.get("OD_CHECKLIST_USERS", ""))


def is_checklist_user(chat_id: int) -> bool:
    return chat_id == OD_CHAT_ID or chat_id in CHECKLIST_USERS


def checklist_user_name(chat_id: int) -> str:
    return CHECKLIST_USERS.get(chat_id, "ОД")


def webapp_base_url() -> str:
    """Публичный HTTPS-адрес этого сервиса. Берём явный WEBAPP_URL,
    иначе домен, который Railway выдаёт автоматически."""
    explicit = os.environ.get("WEBAPP_URL", "").rstrip("/")
    if explicit:
        return explicit
    domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
    if domain:
        return f"https://{domain}"
    # Адрес приложения на Timeweb App Platform
    return "https://florianmarichal612-star-od-bot-4104.twc1.net"
