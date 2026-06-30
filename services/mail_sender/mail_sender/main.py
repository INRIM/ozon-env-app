from __future__ import annotations

import asyncio
import logging
import os
import sys

from .config import MailSenderConfig
from .ozon_gateway import OzonGateway
from .renderer import MailRenderer
from .sender import SmtpSender
from .worker import MailWorker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("mail_sender")


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

    cfg = MailSenderConfig.from_env()
    settings = OzonEnvCoreSettings.from_env()

    env = OzonEnv(cfg=settings.ozon_env_cfg())
    logger.info("init ozon-env (app_code=%s)...", settings.app_code)
    await _init_env_with_retry(
        env,
        attempts=int(os.getenv("MAIL_INIT_RETRIES", "30")),
        delay=float(os.getenv("MAIL_INIT_RETRY_DELAY", "2")),
    )
    try:
        gateway = OzonGateway(
            env,
            app_info={
                "app_name": cfg.app_name,
                "base_url": cfg.base_url,
            },
            app_code=settings.app_code,
        )
        renderer = MailRenderer(
            cfg.base_template_html(), app_name=cfg.app_name
        )
        sender = SmtpSender()
        worker = MailWorker(gateway, renderer, sender)
        await worker.run_forever(cfg.poll_interval)
    finally:
        await env.close_env()


if __name__ == "__main__":
    asyncio.run(main())
