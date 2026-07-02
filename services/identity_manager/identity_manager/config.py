from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class IdentityManagerConfig:
    """Configurazione del worker Identity Manager."""

    sync_interval_minutes: float
    init_retries: int
    init_retry_delay: float

    @classmethod
    def from_env(cls) -> IdentityManagerConfig:
        return cls(
            sync_interval_minutes=float(os.getenv("IDENTITY_MANAGER_INTERVAL_MINUTES", "60")),
            init_retries=int(os.getenv("IDENTITY_MANAGER_INIT_RETRIES", "30")),
            init_retry_delay=float(os.getenv("IDENTITY_MANAGER_INIT_RETRY_DELAY", "2")),
        )
