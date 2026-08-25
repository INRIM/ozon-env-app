import asyncio

from mail_sender import sender as sender_mod
from mail_sender.ozon_gateway import OzonGateway
from mail_sender.renderer import MailRenderer
from mail_sender.sender import SmtpSender
from mail_sender.worker import MailWorker, build_context

BASE = "<base app={{ app_name }}>{{ html|safe }}</base>"


class Rec:
    """CoreModel-like: attributi mutabili + get_dict()."""

    def __init__(self, **fields):
        self.__dict__.update(fields)

    def get_dict(self):
        return dict(self.__dict__)


# --------------------------- fakes: model layer ----------------------------

class FakeModel:
    def __init__(self, find_rows=None, by_name=None):
        self._find_rows = find_rows or []
        self._by_name = by_name or {}
        self.updated = []
        self.last_domain = None

    async def find(self, domain, limit=0):
        self.last_domain = domain
        return list(self._find_rows)

    async def by_name(self, name):
        return self._by_name.get(name)

    async def update(self, record):
        self.updated.append(record)
        return record


class FakeEnv:
    def __init__(self, models):
        self._models = models

    def get(self, name):
        return self._models.get(name)


# --------------------------- fakes: gateway/sender -------------------------

class FakeGateway:
    def __init__(self, messages, templates, servers, records, app_info=None):
        self._messages = messages
        self._templates = templates
        self._servers = servers
        self._records = records
        self._app_info = app_info or {"app_name": "Demo"}
        self.sent = []
        self.errors = []

    async def pending_messages(self):
        return list(self._messages)

    def app_info(self):
        return self._app_info

    async def load_template(self, name):
        return self._templates.get(name)

    async def load_server(self, name):
        return self._servers.get(name)

    async def load_record(self, model, rec_name):
        return self._records.get((model, rec_name))

    async def mark_sent(self, record):
        self.sent.append(record.get_dict().get("rec_name"))

    async def mark_error(self, record, logs):
        self.errors.append((record.get_dict().get("rec_name"), logs))


class FakeSender:
    def __init__(self, raise_exc=None):
        self.calls = []
        self._raise = raise_exc

    def send(self, server, subject, recipients, html):
        self.calls.append(
            {
                "server": server,
                "subject": subject,
                "recipients": recipients,
                "html": html,
            }
        )
        if self._raise:
            raise self._raise


def _gateway(**over):
    base = dict(
        messages=[
            Rec(rec_name="mq_1", mail_template="welcome", rel_rec_name="ord-1",
                stato="da_inviare")
        ],
        templates={
            "welcome": Rec(
                rec_name="welcome", server="s1", model="ordini",
                subject="Ciao {{ data.nome }}",
                recipient="{{ data.owner_mail }}",
                corpoDellaMail="<p>Ordine {{ data.nome }}</p>",
            )
        },
        servers={"s1": Rec(MAIL_SERVER="smtp", MAIL_FROM="a@b.it")},
        records={("ordini", "ord-1"): Rec(nome="Mario",
                                          owner_mail="mario@x.it")},
    )
    base.update(over)
    return FakeGateway(**base)


def _worker(gateway, sender=None):
    return MailWorker(gateway, MailRenderer(BASE, app_name="Demo"),
                      sender or FakeSender())


# ------------------------------- renderer ----------------------------------

def test_renderer_renders_and_wraps():
    r = MailRenderer(BASE, app_name="Demo")
    subject, recipients, html = r.render(
        {
            "subject": "Ciao {{ data.nome }}",
            "recipient": "a@x.it, b@x.it",
            "corpoDellaMail": "<p>{{ data.nome }}</p>",
        },
        {"data": {"nome": "Mario"}, "app": {"app_name": "Demo"}},
    )
    assert subject == "Ciao Mario"
    assert recipients == ["a@x.it", "b@x.it"]
    assert "<p>Mario</p>" in html
    assert "app=Demo" in html


def test_renderer_missing_placeholder_is_empty():
    r = MailRenderer(BASE)
    subject, _, _ = r.render(
        {"subject": "X{{ data.manca.proprio }}Y"}, {"data": {}}
    )
    assert subject == "XY"


def test_build_context_user_from_owner():
    ctx = build_context(
        {"nome": "Mario", "owner_uid": "u1", "owner_mail": "m@x.it"},
        {"app_name": "Demo"},
    )
    assert ctx["data"]["nome"] == "Mario"
    assert ctx["form"] == ctx["data"]
    assert ctx["user"]["uid"] == "u1"
    assert ctx["user"]["mail"] == "m@x.it"
    assert ctx["app"]["app_name"] == "Demo"


# -------------------------------- worker -----------------------------------

def test_process_once_success():
    gw = _gateway()
    sender = FakeSender()
    worker = _worker(gw, sender)

    sent = asyncio.run(worker.process_once())

    assert sent == 1
    assert gw.sent == ["mq_1"]
    assert gw.errors == []
    call = sender.calls[0]
    assert call["subject"] == "Ciao Mario"
    assert call["recipients"] == ["mario@x.it"]
    assert "<p>Ordine Mario</p>" in call["html"]
    assert call["server"]["MAIL_FROM"] == "a@b.it"


def test_process_once_missing_template_marks_error():
    gw = _gateway(templates={})
    worker = _worker(gw)

    sent = asyncio.run(worker.process_once())

    assert sent == 0
    assert gw.sent == []
    assert gw.errors[0][0] == "mq_1"
    assert "non trovato" in gw.errors[0][1]


def test_process_once_no_recipients_marks_error():
    gw = _gateway(
        templates={
            "welcome": Rec(rec_name="welcome", server="s1", model="ordini",
                           subject="x", recipient="", corpoDellaMail="y")
        }
    )
    worker = _worker(gw)

    asyncio.run(worker.process_once())

    assert gw.sent == []
    assert "nessun destinatario" in gw.errors[0][1]


def test_process_once_sender_error_marks_error_with_traceback():
    gw = _gateway()
    sender = FakeSender(raise_exc=RuntimeError("smtp down"))
    worker = _worker(gw, sender)

    asyncio.run(worker.process_once())

    assert gw.sent == []
    assert gw.errors[0][0] == "mq_1"
    assert "smtp down" in gw.errors[0][1]
    assert "RuntimeError" in gw.errors[0][1]


def test_process_once_db_blip_leaves_message_pending():
    """Un AutoReconnect durante la lettura del template non e' un problema
    del messaggio: marcarlo in_errore lo toglierebbe per sempre dalla coda
    (`_pending_domain` ripesca solo i `da_inviare`)."""
    from pymongo.errors import AutoReconnect

    gw = _gateway()

    async def boom(_name):
        raise AutoReconnect("ozonenv_app_db:27017: [Errno 104] Connection reset by peer")

    gw.load_template = boom
    sender = FakeSender()
    worker = _worker(gw, sender)

    sent = asyncio.run(worker.process_once())

    assert sent == 0
    assert gw.sent == []
    assert gw.errors == []          # <- il record resta da_inviare
    assert sender.calls == []


def test_process_once_socket_reset_leaves_message_pending():
    gw = _gateway()

    async def boom(_model, _rec_name):
        raise ConnectionResetError(104, "Connection reset by peer")

    gw.load_record = boom
    worker = _worker(gw)

    asyncio.run(worker.process_once())

    assert gw.sent == []
    assert gw.errors == []


def test_process_once_smtp_failure_still_marks_error():
    """Il guasto SMTP resta in_errore: ritentare un invio gia' partito
    rischia il doppio invio, la lettura sul DB no."""
    gw = _gateway()
    sender = FakeSender(raise_exc=OSError("smtp socket down"))
    worker = _worker(gw, sender)

    asyncio.run(worker.process_once())

    assert gw.sent == []
    assert gw.errors[0][0] == "mq_1"
    assert "smtp socket down" in gw.errors[0][1]


# ------------------------- ozon_gateway (model layer) ----------------------

def test_gateway_pending_uses_model_find_app_code_scoped():
    mq = FakeModel(find_rows=[Rec(rec_name="mq_1", stato="da_inviare")])
    gw = OzonGateway(
        FakeEnv({"message_queue": mq}), {"app_name": "D"}, app_code="demo"
    )

    rows = asyncio.run(gw.pending_messages())

    assert mq.last_domain == {
        "$and": [
            {"stato": "da_inviare"},
            {
                "$or": [
                    {"app_code": "demo"},
                    {"app_code": ""},
                    {"app_code": None},
                    {"app_code": {"$exists": False}},
                ]
            },
        ]
    }
    assert rows[0].get_dict()["rec_name"] == "mq_1"


def test_gateway_pending_without_app_code_is_stato_only():
    mq = FakeModel(find_rows=[])
    gw = OzonGateway(FakeEnv({"message_queue": mq}), {})

    asyncio.run(gw.pending_messages())

    assert mq.last_domain == {"stato": "da_inviare"}


def test_gateway_by_name_returns_coremodel():
    tmpl = FakeModel(by_name={"welcome": Rec(rec_name="welcome", server="s1")})
    gw = OzonGateway(FakeEnv({"mail_template": tmpl}), {}, app_code="demo")

    record = asyncio.run(gw.load_template("welcome"))

    assert record.get_dict()["server"] == "s1"


def test_gateway_mark_error_updates_via_model_update():
    mq = FakeModel()
    gw = OzonGateway(FakeEnv({"message_queue": mq}), {})
    rec = Rec(rec_name="mq_9", stato="da_inviare", logs="")

    asyncio.run(gw.mark_error(rec, "boom"))

    assert mq.updated[0] is rec
    assert rec.stato == "in_errore"
    assert rec.logs == "boom"


# ------------------------------- smtp sender -------------------------------

class FakeSmtp:
    """Stub smtplib client: registra login/send senza rete."""

    instances = []

    def __init__(self, host, port, **kw):
        self.host = host
        self.port = port
        self.logins = []
        self.sent = []
        FakeSmtp.instances.append(self)

    def starttls(self, context=None):
        pass

    def login(self, user, password):
        self.logins.append((user, password))

    def send_message(self, message):
        self.sent.append(message)

    def quit(self):
        pass


def _patch_smtp(monkeypatch):
    FakeSmtp.instances = []
    monkeypatch.setattr(sender_mod.smtplib, "SMTP_SSL", FakeSmtp)
    monkeypatch.setattr(sender_mod.smtplib, "SMTP", FakeSmtp)


_SERVER = {
    "MAIL_SERVER": "smtp.gmail.com",
    "MAIL_FROM": "notifiche@inrim.it",
    "MAIL_SSL": True,
    "port": "465",
    "mailServerUser": "notifiche@inrim.it",
    "MAIL_PASSWORD": "secret",
}


def test_sender_login_when_use_credentials_true(monkeypatch):
    _patch_smtp(monkeypatch)
    server = {**_SERVER, "USE_CREDENTIALS": True}

    SmtpSender().send(server, "S", ["a@x.it"], "<p>hi</p>")

    client = FakeSmtp.instances[0]
    assert client.logins == [("notifiche@inrim.it", "secret")]
    assert client.sent


def test_sender_login_when_creds_present_flag_false(monkeypatch):
    """USE_CREDENTIALS dimenticato a false ma user+password presenti: login
    deve partire comunque (era il caso del 530 con Gmail)."""
    _patch_smtp(monkeypatch)
    server = {**_SERVER, "USE_CREDENTIALS": False}

    SmtpSender().send(server, "S", ["a@x.it"], "<p>hi</p>")

    client = FakeSmtp.instances[0]
    assert client.logins == [("notifiche@inrim.it", "secret")]


def test_sender_no_login_without_credentials(monkeypatch):
    _patch_smtp(monkeypatch)
    server = {k: v for k, v in _SERVER.items()
              if k not in {"mailServerUser", "MAIL_PASSWORD"}}
    server["USE_CREDENTIALS"] = False

    SmtpSender().send(server, "S", ["a@x.it"], "<p>hi</p>")

    client = FakeSmtp.instances[0]
    assert client.logins == []
    assert client.sent
