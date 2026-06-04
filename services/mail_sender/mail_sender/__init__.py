"""mail_sender — worker esterno (pull) che svuota la coda message_queue.

Connette al DB via ozon-env, legge i record `message_queue` in stato
`da_inviare`, renderizza il mail_template (Jinja + base template) coi dati del
record correlato, invia via SMTP usando il mail_server_out, poi aggiorna
`stato` (`inviato`/`in_errore`) e `logs`.
"""
