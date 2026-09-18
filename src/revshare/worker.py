from __future__ import annotations

import asyncio
import logging

from revshare.bot import poll_forever
from revshare.dispatch import send_due_notices
from revshare.fx import fetch_month_average_rates
from revshare.secrets import load_secrets, telegram_token
from revshare.store import Store

log = logging.getLogger("revshare.worker")


async def _notice_scheduler_loop(store: Store) -> None:
    while True:
        try:
            await asyncio.to_thread(send_due_notices, store)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Notice scheduler run failed")
        await asyncio.sleep(3600)


async def _fx_autoload_loop(store: Store) -> None:
    while True:
        try:
            await asyncio.to_thread(store.autoload_reporting_rates)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("FX rate autoload failed")
        await asyncio.sleep(3600)


async def run_worker() -> None:
    load_secrets()
    store = Store(fx_loader=fetch_month_average_rates)
    tasks: list[asyncio.Task] = [
        asyncio.create_task(_notice_scheduler_loop(store)),
        asyncio.create_task(_fx_autoload_loop(store)),
    ]
    if telegram_token():
        tasks.append(asyncio.create_task(poll_forever(store)))
    else:
        log.warning("TELEGRAM_BOT_TOKEN is not configured; Telegram polling is disabled")
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def run() -> None:
    asyncio.run(run_worker())
