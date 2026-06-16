from __future__ import annotations

import asyncio
import getpass
import logging
import os
import sys
from dataclasses import dataclass
from dataclasses import field

from kc_provision import KeycloakAdmin
from kc_provision import KeycloakAdminError

from . import envfile
from . import pipeline

logger = logging.getLogger("keycloak_manager")


@dataclass
class Session:
    server_url: str
    realm: str
    # prefix + env_out sono scelte di pipeline (step 3), non config: cosi una
    # volta messi i secret in .env il service non si tocca piu.
    prefix: str = ""
    env_out: str = "/out/kc-env.var"
    app_client_id: str = ""
    m2m_client_id: str = ""
    m2m_secret: str = ""
    app_audience: str = ""
    assigned: list[str] = field(default_factory=list)

    @property
    def ready_for_env(self) -> bool:
        return bool(self.m2m_client_id and self.m2m_secret and self.app_audience)


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    answer = input(f"{prompt}{suffix}: ").strip()
    return answer or default


def _ask_yes(prompt: str, default: bool = True) -> bool:
    d = "S/n" if default else "s/N"
    answer = input(f"{prompt} [{d}]: ").strip().lower()
    if not answer:
        return default
    return answer in {"s", "si", "y", "yes"}


# --- steps (I/O) --------------------------------------------------------


async def step_app_client(admin: KeycloakAdmin, session: Session) -> None:
    client_id = _ask("clientId del client APP (resource server)", session.app_client_id)
    if not client_id:
        print("annullato.")
        return
    confidential = _ask_yes("confidential? (no = public)", default=True)
    result = await pipeline.ensure_app_client(
        admin, client_id, confidential=confidential
    )
    session.app_client_id = client_id
    print(
        f"  client app '{client_id}' "
        f"{'creato' if result.created else 'gia presente'} (uuid={result.uuid})"
    )


async def step_m2m_client(admin: KeycloakAdmin, session: Session) -> None:
    app_client_id = _ask(
        "clientId del client APP a cui legare l'audience",
        session.app_client_id,
    )
    if not app_client_id:
        print("serve il client app (esegui prima lo step 1).")
        return
    m2m_id = _ask("clientId del client M2M da creare", session.m2m_client_id)
    if not m2m_id:
        print("annullato.")
        return
    scope_name = _ask("nome client-scope audience", f"{m2m_id}-audience")
    extra = _ask(
        "altri client a cui assegnare l'audience (CSV, opzionale)", ""
    )
    assign_to = [m2m_id] + [c.strip() for c in extra.split(",") if c.strip()]

    m2m = await pipeline.ensure_m2m_client(admin, m2m_id)
    aud = await pipeline.bind_client_audience(
        admin,
        scope_name=scope_name,
        app_client_id=app_client_id,
        assign_to_client_ids=assign_to,
    )
    session.app_client_id = app_client_id
    session.m2m_client_id = m2m_id
    session.m2m_secret = m2m.secret
    session.app_audience = app_client_id  # client-audience: aud = clientId app
    session.assigned = aud.assigned
    print(
        f"  client M2M '{m2m_id}' {'creato' if m2m.created else 'gia presente'}; "
        f"audience (aud={app_client_id}) assegnata a: {', '.join(aud.assigned)}"
    )


def step_generate_env(session: Session) -> None:
    if not session.ready_for_env:
        print("manca il client M2M: esegui prima lo step 2.")
        return
    session.prefix = _ask(
        "prefisso env var (vuoto = OAUTH_*, es SCHEDULER)", session.prefix
    )
    session.env_out = _ask("file di output", session.env_out)
    env_vars = envfile.build_env_vars(
        prefix=session.prefix,
        server_url=session.server_url,
        realm=session.realm,
        m2m_client_id=session.m2m_client_id,
        m2m_secret=session.m2m_secret,
        app_audience=session.app_audience,
    )
    envfile.write_env_file(session.env_out, env_vars)
    print(f"\n  scritto {session.env_out}:")
    for k, v in env_vars.items():
        shown = v if "SECRET" not in k else "***"
        print(f"    {k}={shown}")
    print(
        "\n  ⚠️ TOKEN_AUDIENCE -> OZON_TOKEN_AUDIENCE lato app: abilita la "
        "verifica aud SOLO dopo che tutti i client emettono l'aud."
    )


# --- main loop ----------------------------------------------------------


def _verify_tls() -> bool:
    return os.getenv("KC_VERIFY_TLS", "true").lower() not in {"0", "false", "no"}


def raw_m2m_admin_kwargs(
    server_url: str,
    realm: str,
    *,
    client_id: str,
    client_secret: str,
    admin_realm: str,
    verify: bool,
) -> dict:
    """kwargs per python-keycloak in modalita client_credentials (service
    account, es `admin-rest-client`): niente user/password."""
    return {
        "server_url": server_url.rstrip("/") + "/",
        "realm_name": realm,
        "user_realm_name": admin_realm,
        "client_id": client_id,
        "client_secret_key": client_secret,
        "grant_type": "client_credentials",
        "verify": verify,
    }


def _build_admin(session_env: dict) -> KeycloakAdmin:
    server = session_env["server_url"]
    realm = session_env["realm"]
    admin_realm = os.getenv("KC_ADMIN_REALM", "master")

    # Preferito: service account (client_credentials), niente credenziali umane.
    client_secret = os.getenv("KC_ADMIN_CLIENT_SECRET", "")
    if client_secret:
        from keycloak import KeycloakAdmin as RawAdmin

        raw = RawAdmin(
            **raw_m2m_admin_kwargs(
                server,
                realm,
                client_id=os.getenv("KC_ADMIN_CLIENT_ID", "admin-rest-client"),
                client_secret=client_secret,
                admin_realm=admin_realm,
                verify=_verify_tls(),
            )
        )
        return KeycloakAdmin(
            server, realm, admin_user="", admin_password="", kc=raw
        )

    # Fallback: password grant (admin-cli).
    admin_user = os.getenv("KC_ADMIN_USER", "")
    admin_pass = os.getenv("KC_ADMIN_PASSWORD", "")
    if not admin_user and sys.stdin.isatty():
        admin_user = input("Keycloak admin user: ").strip()
    if not admin_pass and sys.stdin.isatty():
        admin_pass = getpass.getpass("Keycloak admin password: ")
    if not admin_user or not admin_pass:
        raise SystemExit(
            "Auth admin richiesta: KC_ADMIN_CLIENT_SECRET (service account) "
            "oppure KC_ADMIN_USER/KC_ADMIN_PASSWORD"
        )
    return KeycloakAdmin(
        server,
        realm,
        admin_user=admin_user,
        admin_password=admin_pass,
        admin_realm=admin_realm,
        verify_tls=_verify_tls(),
    )


async def _amain() -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)
    server = os.getenv("KC_SERVER_URL", "").strip()
    realm = os.getenv("KC_REALM", "").strip()
    if not server or not realm:
        print("KC_SERVER_URL e KC_REALM richiesti (.env)", file=sys.stderr)
        return 2
    session = Session(server_url=server, realm=realm)

    admin = _build_admin({"server_url": server, "realm": realm})
    try:
        await admin.login()
    except KeycloakAdminError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(f"\nKeycloak manager — realm '{realm}' @ {server}")
    try:
        while True:
            print(
                "\n  1) Aggiungi client APP\n"
                "  2) Aggiungi client M2M -> client APP (+ audience)\n"
                "  3) Genera kc-env.var\n"
                "  0) Esci"
            )
            choice = input("scelta: ").strip()
            try:
                if choice == "1":
                    await step_app_client(admin, session)
                elif choice == "2":
                    await step_m2m_client(admin, session)
                elif choice == "3":
                    step_generate_env(session)
                elif choice == "0":
                    break
                else:
                    print("scelta non valida.")
            except KeycloakAdminError as exc:
                print(f"errore: {exc}", file=sys.stderr)
    finally:
        await admin.aclose()
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
