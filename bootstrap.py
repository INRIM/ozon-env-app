#!/usr/bin/env python
"""Install base and external plugins into MongoDB, then seed app settings.

Reads connection settings from .env-local / .env / environment variables.

Usage:
    uv run python bootstrap.py --admin UID
    uv run python bootstrap.py --admin UID --base-only
    uv run python bootstrap.py --admin UID --plugins-dir /path
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    stream=sys.stdout,
)

log = logging.getLogger(__name__)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--admin",
        required=True,
        metavar="UID",
        help="UID dell'admin base: aggiunto a group_users(group=admin) per app_code "
        "(fonte di is_admin) e, per storico, a settings.admins.",
    )
    parser.add_argument(
        "--base-only",
        action="store_true",
        help="Installa solo il plugin base built-in.",
    )
    parser.add_argument(
        "--plugins-dir",
        default=None,
        metavar="PATH",
        help="Override PLUGINS_FOLDER env var.",
    )
    return parser.parse_args()


async def _seed_settings(cfg: dict, env_settings, admin_uid: str) -> None:
    from ozonenv.OzonEnv import OzonEnv
    from app.app_settings import build_public_db_settings_payload

    app_code = env_settings.app_code
    env = OzonEnv(cfg=cfg)
    log.info("seed: init env...")
    await env.init_env()
    try:
        m_settings = env.get("settings")
        log.info("seed: by_name(%s)...", app_code)
        existing = await m_settings.by_name(app_code)
        if existing is not None:
            # DB is authoritative — preserve all existing fields, only merge admins.
            current_admins = list(getattr(existing, "admins", []) or [])
            if admin_uid not in current_admins:
                current_admins.append(admin_uid)
                setattr(existing, "admins", current_admins)
                await m_settings.update(existing)
                log.info("seed: settings.admins updated: %s", current_admins)
            else:
                log.info("seed: settings already up-to-date, admins=%s", current_admins)
        else:
            # No record yet: write full payload from env vars.
            payload = build_public_db_settings_payload(env_settings)
            admins = list(payload.get("admins") or [])
            if admin_uid not in admins:
                admins.append(admin_uid)
            payload["admins"] = admins
            new_rec = await m_settings.new(data=payload)
            await m_settings.insert(new_rec)
            log.info(
                "seed: settings created for app_code=%s admins=%s", app_code, admins
            )
    finally:
        await env.close_env()


async def _seed_admin_group(cfg: dict, env_settings, admin_uid: str) -> None:
    """Grant admin_uid membership in the 'admin' group_users record for
    app_code. is_admin is sourced live from group_users, not settings.admins
    (see app.ozon_env_acl.get_admin_uids / apply_session_groups) — this is
    now the authoritative way to bootstrap an admin.
    """
    from ozonenv.OzonEnv import OzonEnv
    from app.ozon_env_acl import ADMIN_GROUP_NAME

    app_code = env_settings.app_code
    rec_name = f"{ADMIN_GROUP_NAME}-{app_code}"
    env = OzonEnv(cfg=cfg)
    log.info("seed: group_users admin init env...")
    await env.init_env()
    try:
        group_users_model = env.get("group_users")
        existing = await group_users_model.load({"rec_name": rec_name})
        if existing:
            current_users = list(getattr(existing, "users", []) or [])
            if admin_uid not in current_users:
                current_users.append(admin_uid)
                setattr(existing, "users", current_users)
                await group_users_model.update(existing)
                log.info(
                    "seed: group_users admin updated app_code=%s users=%s",
                    app_code,
                    current_users,
                )
            else:
                log.info(
                    "seed: group_users admin already includes %s (app_code=%s)",
                    admin_uid,
                    app_code,
                )
            return

        payload = {
            "rec_name": rec_name,
            "label": "Admin",
            "app_code": app_code,
            "group": ADMIN_GROUP_NAME,
            "users": [admin_uid],
            "active": True,
            "deleted": 0,
            "default": False,
            "demo": False,
            "list_order": 1,
            "parent": "",
            "process_id": "",
            "process_task_id": "",
            "sys": False,
            "type": "form",
            "data_value": {
                "data_model": "group_users",
                "rec_name": rec_name,
                "label": "Admin",
            },
        }
        new_rec = await group_users_model.new(data=payload)
        await group_users_model.insert(new_rec)
        log.info(
            "seed: group_users admin created app_code=%s users=%s",
            app_code,
            [admin_uid],
        )
    finally:
        await env.close_env()


async def run(args: argparse.Namespace) -> None:
    from app.app_settings import get_env_settings
    from app.deps.app_env import _build_ozon_cfg
    from app.plugins import _BASE_PLUGIN, discover_plugins
    from app.services.plugin_installer import PluginInstaller

    log.info("=== BOOTSTRAP START ===")

    log.info("[1/4] carico settings da env...")
    settings = get_env_settings()
    if not settings.app_code:
        log.error("APP_CODE non configurato — abort")
        sys.exit(1)
    log.info("[1/4] app_code=%s admin=%s", settings.app_code, args.admin)

    log.info("[2/4] scopro plugin da %s ...", settings.plugins_folder if not args.plugins_dir else args.plugins_dir)
    plugins_dir = Path(args.plugins_dir) if args.plugins_dir else settings.plugins_folder
    plugins = [_BASE_PLUGIN] if args.base_only else discover_plugins(
        plugins_dir=plugins_dir, app_code=settings.app_code
    )
    log.info("[2/4] %d plugin(s): %s", len(plugins), [p.name for p in plugins])

    log.info("[3/4] installo plugin nel DB...")
    cfg = _build_ozon_cfg()
    await PluginInstaller(cfg=cfg, app_code=settings.app_code).run(plugins)
    log.info("[3/4] plugin installati")

    log.info("[4/4] seed settings uid=%s ...", args.admin)
    await _seed_settings(cfg, settings, args.admin)
    await _seed_admin_group(cfg, settings, args.admin)
    log.info("[4/4] settings e group_users admin aggiornati")

    log.info("=== BOOTSTRAP COMPLETE ===")


def main() -> None:
    asyncio.run(run(_parse_args()))
    os._exit(0)


if __name__ == "__main__":
    main()
