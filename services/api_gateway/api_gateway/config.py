from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

DEFAULT_CONFIG_PATH = "/app/config/gateway.json"

# `${VAR}` nei valori stringa del config: le credenziali restano in env
# (service.env), il JSON dichiara solo DOVE vanno.
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass(frozen=True)
class GatewayConfig:
    """Config di processo del gateway, da env var.

    `s2s_token` e' la credenziale che ozon-env-app presenta sull'header
    `s2s_header` (lato app: REMOTE_SELECT_S2S_TOKEN / REMOTE_SELECT_S2S_HEADER).
    Senza token configurato il service NON parte: un gateway che espone le
    API esterne in chiaro sulla rete docker sarebbe peggio di un service
    giu' e visibile.
    """

    s2s_token: str
    s2s_header: str
    config_path: str
    http_timeout: float

    @classmethod
    def from_env(cls) -> "GatewayConfig":
        return cls(
            s2s_token=os.getenv("API_GATEWAY_S2S_TOKEN", ""),
            s2s_header=os.getenv(
                "API_GATEWAY_S2S_HEADER", "x-ozon-s2s-token"
            ).lower(),
            config_path=os.getenv("API_GATEWAY_CONFIG", DEFAULT_CONFIG_PATH),
            http_timeout=float(os.getenv("API_GATEWAY_HTTP_TIMEOUT", "30")),
        )

    def validate(self) -> None:
        if not self.s2s_token:
            raise ValueError(
                "API_GATEWAY_S2S_TOKEN mancante: il gateway non espone dati "
                "senza autenticazione service-to-service"
            )
        if not self.s2s_header:
            raise ValueError("API_GATEWAY_S2S_HEADER non puo' essere vuoto")


class ResourceConfig(BaseModel):
    """Una risorsa esposta: l'elenco delle risorse e' la whitelist, il
    gateway non inoltra path liberi verso l'upstream.

    `path` va copiato VERBATIM dall'API esterna: gli slash finali non sono
    uniformi (People: `/get_rooms` senza, `/getsectors/` con) e sbagliarli
    produce un 307 o un 404.
    """

    model_config = ConfigDict(extra="forbid")

    path: str
    # POST rifiutato (405) se non abilitato qui: e' il config, non il
    # chiamante, a decidere quali risorse accettano un body.
    allow_post: bool = False
    description: str = ""

    @field_validator("path")
    @classmethod
    def _path_absolute(cls, v: str) -> str:
        if not v.startswith("/"):
            raise ValueError(f"path deve iniziare con '/': {v!r}")
        return v


class ClientCredentialsAuth(BaseModel):
    """Bearer JWT via client_credentials (M2M keycloak), alternativo agli
    header statici."""

    model_config = ConfigDict(extra="forbid")

    type: str = "client_credentials"
    token_url: str
    client_id: str
    client_secret: str
    audience: str = ""
    scope: str = ""

    @field_validator("type")
    @classmethod
    def _known_type(cls, v: str) -> str:
        if v != "client_credentials":
            raise ValueError(f"auth.type non supportato: {v!r}")
        return v


class UpstreamConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str
    headers: dict[str, str] = Field(default_factory=dict)
    auth: ClientCredentialsAuth | None = None
    # Solo GET. 0 = cache disabilitata.
    cache_ttl: float = 300.0
    timeout: float | None = None
    resources: dict[str, ResourceConfig]

    @field_validator("base_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError(f"base_url deve essere http(s): {v!r}")
        return v.rstrip("/")


class UpstreamsFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    upstreams: dict[str, UpstreamConfig]


def _expand_env(value: Any, env: dict[str, str], where: str) -> Any:
    if isinstance(value, str):

        def repl(match: re.Match[str]) -> str:
            name = match.group(1)
            resolved = env.get(name, "")
            # Una var mancante NON diventa stringa vuota: un header x-key
            # vuoto darebbe 401 dall'upstream e una select vuota, senza
            # nessun segnale. Meglio non partire.
            if not resolved:
                raise ValueError(f"env var {name} mancante o vuota ({where})")
            return resolved

        return _ENV_REF.sub(repl, value)
    if isinstance(value, dict):
        return {
            k: _expand_env(v, env, f"{where}.{k}") for k, v in value.items()
        }
    if isinstance(value, list):
        return [_expand_env(v, env, f"{where}[{i}]") for i, v in enumerate(value)]
    return value


def parse_upstreams(
    raw: dict[str, Any], env: dict[str, str] | None = None
) -> dict[str, UpstreamConfig]:
    expanded = _expand_env(raw, dict(os.environ) if env is None else env, "config")
    return UpstreamsFile.model_validate(expanded).upstreams


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"config non trovato: {path}") from exc
    except ValueError as exc:
        raise ValueError(f"config non JSON valido: {path}: {exc}") from exc


def load_upstreams(
    path: str, env: dict[str, str] | None = None
) -> dict[str, UpstreamConfig]:
    """Carica `gateway.json`: `{"upstreams": {"<nome>": "<file>.json"}}`.

    Ogni upstream sta in un file suo, accanto a gateway.json: aggiungere o
    togliere un'API esterna non tocca la config delle altre. Un valore
    oggetto al posto del nome file e' accettato come config inline.
    """

    config_file = Path(path)
    raw = _read_json(config_file)
    if not isinstance(raw, dict) or not isinstance(raw.get("upstreams"), dict):
        raise ValueError(f"{config_file}: atteso {{\"upstreams\": {{...}}}}")
    resolved: dict[str, Any] = {}
    for name, ref in raw["upstreams"].items():
        if isinstance(ref, str):
            # Solo file nella stessa cartella: il riferimento non deve
            # poter leggere file arbitrari del container.
            if Path(ref).name != ref or ref in {".", ".."}:
                raise ValueError(
                    f"upstream {name}: {ref!r} deve essere un nome file "
                    f"nella cartella di {config_file.name}"
                )
            resolved[name] = _read_json(config_file.parent / ref)
        else:
            resolved[name] = ref
    return parse_upstreams({**raw, "upstreams": resolved}, env)
