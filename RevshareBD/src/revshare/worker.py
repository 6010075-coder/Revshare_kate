from __future__ import annotations

import asyncio
import logging

from revshare.bot import poll_forever
from revshare.dispatch import send_due_notices
from revshare.fx import fetch_month_average_rates
from revshare.altegio_sync import sync_period
from revshare.notifications import reporting_period
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


async def _altegio_sync_loop(store: Store) -> None:
    """Refresh Altegio usage before reports are reviewed.

    The worker is deliberately the only background process that runs this job.
    This keeps multiple web workers from competing for the same read-only BI
    connection while still refreshing the current closed reporting month.
    """
    while True:
        try:
            period = reporting_period()
            result = await asyncio.to_thread(sync_period, store, period)
            log.info(
                "Altegio usage sync finished: period=%s synced=%s skipped=%s errors=%s",
                period,
                result.partners_synced,
                len(result.partners_skipped),
                len(result.errors),
            )
        except asyncio.CancelledError:
            raise
        except RuntimeError as error:
            if str(error) == "altegio_mysql_missing":
                log.warning(
                    "Altegio usage sync is waiting for read-only BI credentials"
                )
            else:
                log.exception("Altegio usage sync failed")
        except Exception:
            log.exception("Altegio usage sync failed")
        await asyncio.sleep(3600)


async def run_worker() -> None:
    load_secrets()
    store = Store(fx_loader=fetch_month_average_rates)
    tasks: list[asyncio.Task] = [
        asyncio.create_task(_notice_scheduler_loop(store)),
        asyncio.create_task(_fx_autoload_loop(store)),
        asyncio.create_task(_altegio_sync_loop(store)),
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
