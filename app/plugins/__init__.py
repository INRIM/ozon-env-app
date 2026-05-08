from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger("uvicorn.error")

# Built-in base plugin — always loaded first, embedded in the image
_BASE_PLUGIN = Path(__file__).parent.parent / "base"


def _read_config(plugin_dir: Path) -> dict:
    config_path = plugin_dir / "config.json"
    if not config_path.exists():
        return {}
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _topo_sort(plugins: dict[str, Path]) -> list[Path]:
    configs = {name: _read_config(path) for name, path in plugins.items()}
    visited: set[str] = set()
    result: list[str] = []

    def visit(name: str) -> None:
        if name in visited:
            return
        visited.add(name)
        for dep in configs.get(name, {}).get("depends", []):
            if dep in plugins:
                visit(dep)
            else:
                logger.warning(
                    "plugin '%s' depends on '%s' which is not installed", name, dep
                )
        result.append(name)

    for name in sorted(plugins):
        visit(name)

    return [plugins[name] for name in result]


def discover_plugins(
    plugins_dir: Path = Path("/plugins"),
    app_code: str = "",
) -> list[Path]:
    result: list[Path] = [_BASE_PLUGIN]

    if not plugins_dir.exists():
        logger.warning(
            "plugins dir '%s' not found, no external plugins loaded", plugins_dir
        )
        return result

    external: dict[str, Path] = {
        entry.name: entry
        for entry in plugins_dir.iterdir()
        if entry.is_dir() and (entry / "config.json").exists()
    }

    if not external:
        logger.info("no external plugins found in '%s'", plugins_dir)
        return result

    if app_code and app_code not in external:
        logger.warning(
            "expected plugin '%s' (APP_CODE) not found in '%s'", app_code, plugins_dir
        )

    result.extend(_topo_sort(external))
    return result
