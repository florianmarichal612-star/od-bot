"""Локальный запуск только веб-части (без Telegram-бота) для проверки Mini App в браузере."""
import asyncio
import os

os.environ.setdefault("CHECKLIST_DEV_MODE", "1")
os.environ.setdefault("CHECKLIST_DB_PATH", "./_dev_checklist.db")

from checklist_web import start_web_server


async def main():
    port = int(os.environ.get("PORT", "8091"))
    await start_web_server(port)
    print(f"dev web server on :{port}")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
