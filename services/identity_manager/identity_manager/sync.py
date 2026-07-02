from __future__ import annotations

import json
import logging
import yaml
from typing import Any

logger = logging.getLogger("identity_manager")


def parse_mongo_query(rule: Any, rec_name: str) -> dict | None:
    """Tenta di decodificare la stringa rule in un dizionario MongoDB valido.
    Supporta sia JSON standard sia formati YAML/JSON5 più rilassati (es. chiavi non virgolettate, apici singoli).
    """
    if isinstance(rule, dict):
        return rule
    if not isinstance(rule, str):
        return None

    rule_str = rule.strip()
    if not rule_str:
        return None

    # Deve apparire come una query MongoDB strutturata (solitamente racchiusa tra parentesi graffe)
    if not (rule_str.startswith("{") and rule_str.endswith("}")):
        logger.warning(
            "La rule per group_users=%s non ha la struttura di una query MongoDB (mancano le parentesi graffe): %s",
            rec_name,
            rule_str,
        )
        return None

    # 1. Prova con JSON standard
    try:
        res = json.loads(rule_str)
        if isinstance(res, dict):
            return res
    except Exception:
        pass

    # 2. Prova con YAML (molto tollerante su apici singoli e chiavi non quotate)
    try:
        res = yaml.safe_load(rule_str)
        if isinstance(res, dict):
            return res
    except Exception as exc:
        logger.warning(
            "Impossibile decodificare la rule per group_users=%s come dizionario JSON o YAML valido: %s. Errore: %s",
            rec_name,
            rule_str,
            exc,
        )

    return None


class IdentitySyncService:
    """Servizio per la sincronizzazione dinamica dei membri dei gruppi (group_users)."""

    def __init__(self, env: Any) -> None:
        self.env = env

    def _get(self, name: str) -> Any:
        model = self.env.get(name)
        if model is None:
            logger.warning("model '%s' non registrato in ozon-env", name)
        return model

    async def run_sync(self) -> None:
        group_users_model = self._get("group_users")
        if group_users_model is None:
            return

        user_model = self._get("user")
        if user_model is None:
            return

        records = await group_users_model.find(domain={"deleted": 0, "active": True})
        logger.info("trovati %d record group_users attivi", len(records))

        for record in records:
            rule_val = getattr(record, "rule", None)
            if not rule_val:
                data_val = getattr(record, "data_value", None) or {}
                if isinstance(data_val, dict):
                    rule_val = data_val.get("rule", None)

            if not rule_val:
                continue

            query_dict = parse_mongo_query(rule_val, record.rec_name)
            if query_dict is None or not isinstance(query_dict, dict) or not query_dict:
                # Salta se non è stato possibile decodificare un dizionario non vuoto
                continue

            logger.info("valuto rule %s per group_users=%s", query_dict, record.rec_name)
            user_query = {"$and": [{"deleted": 0}, {"active": True}, query_dict]}
            try:
                matching_users = await user_model.find(domain=user_query, limit=0)
            except Exception as exc:
                logger.error(
                    "errore durante la ricerca degli utenti per group_users=%s (query=%s): %s",
                    record.rec_name,
                    user_query,
                    exc,
                )
                continue

            user_names = sorted([u.rec_name for u in matching_users if getattr(u, "rec_name", None)])
            current_users = sorted(getattr(record, "users", None) or [])

            if user_names == current_users:
                logger.info("lista utenti invariata per group_users=%s", record.rec_name)
                continue

            logger.info(
                "aggiorno utenti per group_users=%s: %s -> %s",
                record.rec_name,
                current_users,
                user_names,
            )

            record.users = user_names
            try:
                await group_users_model.update(record)
            except Exception as exc:
                logger.error(
                    "fallito aggiornamento utenti per group_users=%s: %s",
                    record.rec_name,
                    exc,
                )
