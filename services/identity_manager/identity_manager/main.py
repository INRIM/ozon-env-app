from __future__ import annotations

import asyncio
import logging
import sys

from .config import IdentityManagerConfig
from .sync import IdentitySyncService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("identity_manager")


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


async def main() -> None:
    from ozonenv.OzonEnv import OzonEnv
    from ozonenv.core.BaseModels import OzonEnvCoreSettings

    cfg = IdentityManagerConfig.from_env()
    settings = OzonEnvCoreSettings.from_env()

    if cfg.sync_interval_minutes <= 0:
        logger.info("identity_manager disattivato (sync_interval_minutes <= 0)")
        return

    env = OzonEnv(cfg=settings.ozon_env_cfg())
    logger.info("init ozon-env (app_code=%s)...", settings.app_code)
    await _init_env_with_retry(
        env, attempts=cfg.init_retries, delay=cfg.init_retry_delay
    )
    
    sync_service = IdentitySyncService(env)

    try:
        logger.info(
            "identity_manager avviato (interval=%s min)",
            cfg.sync_interval_minutes,
        )
        while True:
            try:
                logger.info("esecuzione ciclo di sincronizzazione group_users...")
                await sync_service.run_sync()
            except Exception:
                logger.exception("errore nel ciclo di sincronizzazione")
            
            await asyncio.sleep(cfg.sync_interval_minutes * 60)
    finally:
        await env.close_env()


if __name__ == "__main__":
    asyncio.run(main())
