from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

from .auth import M2MTokenProvider
from .config import SchedulerConfig
from .gateway import CalendarGateway
from .health import write_heartbeat
from .scheduler import CalendarScheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("calendar_scheduler")


async def _init_env_with_retry(env, *, attempts: int, delay: float) -> None:
    for attempt in range(1, attempts + 1):
        try:
            await env.init_env()
            return
        except Exception:
            if attempt >= attempts:
                raise
            logger.exception(
                "init ozon-env fallito, retry %s/%s tra %ss",
                attempt,
                attempts,
                delay,
            )
            await asyncio.sleep(delay)


async def _run(cfg: SchedulerConfig, env) -> None:
    token_provider = M2MTokenProvider(
        cfg.oauth_token_url,
        cfg.oauth_client_id,
        cfg.oauth_client_secret,
        audience=cfg.oauth_audience,
        scope=cfg.oauth_scope,
    )
    gateway = CalendarGateway(
        env,
        run_base_url=cfg.run_base_url,
        token_provider=token_provider,
        lock_ttl_seconds=cfg.lock_ttl_seconds,
        timezone=cfg.timezone,
    )
    scheduler = CalendarScheduler(gateway, cfg)
    scheduler.start()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - non-unix
            pass

    logger.info(
        "calendar_scheduler avviato run_base_url=%s poll=%ss",
        cfg.run_base_url,
        cfg.poll_interval,
    )
    try:
        while not stop.is_set():
            try:
                count = await scheduler.sync_once()
                write_heartbeat(cfg.health_file)
                logger.debug("sync completato task=%s", count)
            except Exception:  # noqa: BLE001 - il loop di sync non deve morire
                logger.exception("errore nel sync calendar")
            try:
                await asyncio.wait_for(stop.wait(), timeout=cfg.poll_interval)
            except asyncio.TimeoutError:
                pass
    finally:
        logger.info("calendar_scheduler shutdown...")
        scheduler.shutdown()
        await gateway.aclose()
        await token_provider.aclose()


async def main() -> None:
    from ozonenv.OzonEnv import OzonEnv
    from ozonenv.core.BaseModels import OzonEnvCoreSettings

    cfg = SchedulerConfig.from_env()
    cfg.validate()

    # Mongo/modelli via ozon-env. Agnostico: l'app_code di bootstrap serve solo
    # allo stub app_settings, i record calendar non sono filtrati per app_code.
    settings = OzonEnvCoreSettings.from_env()
    env = OzonEnv(cfg=settings.ozon_env_cfg())
    logger.info("init ozon-env...")
    await _init_env_with_retry(
        env,
        attempts=int(os.getenv("SCHEDULER_INIT_RETRIES", "30")),
        delay=float(os.getenv("SCHEDULER_INIT_RETRY_DELAY", "2")),
    )
    try:
        await _run(cfg, env)
    finally:
        await env.close_env()


if __name__ == "__main__":
    asyncio.run(main())
