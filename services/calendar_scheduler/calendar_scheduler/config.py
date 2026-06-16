from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class SchedulerConfig:
    """Config del worker. Agnostico sull'app_code: i dati calendar di tutti gli
    app_code vivono su un'unica Mongo (letta via ozon-env). L'`app_code` per la
    run viene dal singolo record, non da qui.

    Connessione Mongo: gestita da ozon-env (`OzonEnvCoreSettings.from_env`,
    variabili `MONGO_*`/`MODELS_FOLDER`). Qui solo i parametri del worker.
    """

    run_base_url: str
    # Auth keycloak M2M (client_credentials). L'endpoint run verifica il bearer
    # come JWT keycloak: niente token statico. Unico segreto: oauth_client_secret.
    oauth_token_url: str
    oauth_client_id: str
    oauth_client_secret: str
    oauth_audience: str
    oauth_scope: str
    poll_interval: float
    lock_ttl_seconds: int
    http_timeout: float
    timezone: str
    jobstore_url: str
    misfire_grace_time: int
    health_file: str

    @classmethod
    def from_env(cls) -> "SchedulerConfig":
        # Nessun segreto versionato: client_secret da secret runtime/env.
        return cls(
            run_base_url=os.getenv("SCHEDULER_RUN_BASE_URL", "").rstrip("/"),
            oauth_token_url=os.getenv("SCHEDULER_OAUTH_TOKEN_URL", ""),
            oauth_client_id=os.getenv("SCHEDULER_OAUTH_CLIENT_ID", ""),
            oauth_client_secret=os.getenv("SCHEDULER_OAUTH_CLIENT_SECRET", ""),
            oauth_audience=os.getenv("SCHEDULER_OAUTH_AUDIENCE", ""),
            oauth_scope=os.getenv("SCHEDULER_OAUTH_SCOPE", ""),
            poll_interval=float(os.getenv("SCHEDULER_POLL_INTERVAL", "45")),
            lock_ttl_seconds=int(os.getenv("SCHEDULER_LOCK_TTL", "1800")),
            http_timeout=float(os.getenv("SCHEDULER_HTTP_TIMEOUT", "30")),
            timezone=os.getenv("SCHEDULER_TZ", "Europe/Rome"),
            jobstore_url=os.getenv(
                "SCHEDULER_JOBSTORE_URL",
                "sqlite:////data/calendar_scheduler.sqlite",
            ),
            misfire_grace_time=int(
                os.getenv("SCHEDULER_MISFIRE_GRACE", "300")
            ),
            health_file=os.getenv(
                "SCHEDULER_HEALTH_FILE", "/tmp/calendar_scheduler_health"
            ),
        )

    def validate(self) -> None:
        if not self.run_base_url:
            raise ValueError("SCHEDULER_RUN_BASE_URL is required")
        missing = [
            name
            for name, value in (
                ("SCHEDULER_OAUTH_TOKEN_URL", self.oauth_token_url),
                ("SCHEDULER_OAUTH_CLIENT_ID", self.oauth_client_id),
                ("SCHEDULER_OAUTH_CLIENT_SECRET", self.oauth_client_secret),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                "keycloak M2M config required (l'endpoint run verifica JWT "
                f"keycloak, nessun token statico): manca {', '.join(missing)}"
            )
