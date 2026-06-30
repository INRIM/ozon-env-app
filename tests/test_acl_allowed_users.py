from __future__ import annotations

from types import SimpleNamespace

from app.ozon_env_acl import apply_session_allowed_users


def _session(uid, is_admin, user):
    return SimpleNamespace(uid=uid, is_admin=is_admin, user=user)


def test_admin_gets_admins_by_default():
    user = {"uid": "a.admin", "allowed_users": ["old"]}
    session = _session("a.admin", True, user)

    allowed = apply_session_allowed_users(
        session, ["a.admin", "b.boss", "a.admin"]
    )

    # admin -> lista admin (dedup, sorted), ignora il vecchio allowed_users
    assert allowed == ["a.admin", "b.boss"]
    assert session.user["allowed_users"] == ["a.admin", "b.boss"]


def test_non_admin_uses_acl_plus_self():
    user = {"uid": "u.utente", "allowed_users": ["resp1", "resp2"]}
    session = _session("u.utente", False, user)

    allowed = apply_session_allowed_users(session, ["a.admin"])

    # non admin -> ACL esistenti + proprio uid, niente admin
    assert allowed == ["resp1", "resp2", "u.utente"]
    assert "a.admin" not in allowed


def test_non_admin_empty_acl_defaults_to_self():
    user = {"uid": "u.solo"}
    session = _session("u.solo", False, user)

    allowed = apply_session_allowed_users(session, ["a.admin"])

    assert allowed == ["u.solo"]


def test_user_not_dict_is_safe():
    session = _session("x", True, None)
    assert apply_session_allowed_users(session, ["a.admin"]) == []
