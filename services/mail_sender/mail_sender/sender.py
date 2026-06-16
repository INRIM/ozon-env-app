from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from typing import Any


def _flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


class MailError(Exception):
    """Errore funzionale durante render/preparazione/invio mail."""


class SmtpSender:
    """Invio SMTP via smtplib mappando i campi di `mail_server_out`.

    Sostituisce fastapi_mail del vecchio sistema con la stdlib: nessuna
    dipendenza extra, e gli stessi parametri TLS/SSL/credenziali.
    """

    def send(
        self,
        server: dict[str, Any],
        subject: str,
        recipients: list[str],
        html_body: str,
    ) -> None:
        host = str(server.get("MAIL_SERVER") or "").strip()
        if not host:
            raise MailError("mail_server_out senza MAIL_SERVER")
        mail_from = str(server.get("MAIL_FROM") or "").strip()
        if not mail_from:
            raise MailError("mail_server_out senza MAIL_FROM")
        if not recipients:
            raise MailError("nessun destinatario")

        use_ssl = _flag(server.get("MAIL_SSL"))
        use_tls = _flag(server.get("MAIL_TLS"))
        port = int(str(server.get("port") or "").strip() or (465 if use_ssl else 587))

        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = mail_from
        message["To"] = ", ".join(recipients)
        message.set_content(
            "Questa email richiede un client che supporti l'HTML."
        )
        message.add_alternative(html_body, subtype="html")

        context = ssl.create_default_context()
        if not _flag(server.get("VALIDATE_CERTS")):
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE

        if use_ssl:
            client = smtplib.SMTP_SSL(host, port, context=context, timeout=30)
        else:
            client = smtplib.SMTP(host, port, timeout=30)
        try:
            if not use_ssl and use_tls:
                client.starttls(context=context)
            # Auth se richiesta dal flag o se le credenziali sono comunque
            # presenti: i server reali (Gmail/SMTP) la pretendono sempre, e
            # un USE_CREDENTIALS dimenticato a false dava 530 con creds valide.
            user = str(server.get("mailServerUser") or "")
            password = str(server.get("MAIL_PASSWORD") or "")
            if _flag(server.get("USE_CREDENTIALS")) or (user and password):
                client.login(user, password)
            client.send_message(message)
        finally:
            try:
                client.quit()
            except Exception:
                pass
