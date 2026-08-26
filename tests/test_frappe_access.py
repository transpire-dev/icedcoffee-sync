"""Role, permission, user, and submit-probe provisioning against a fake client.

Every write is recorded by a fake rather than asserted through a mocked return
value: the point of `grant` is which ptypes it sends and with what value, and
the point of `probe_submit` is which local guard it lifts and how it classifies
the server's response -- both are decisions about real logic, not behaviour a
mock could fake its way past.
"""

from __future__ import annotations

from typing import Any

import pytest
from stolon.frappe.access import (
    _PERMISSION_MANAGER,
    PTYPES,
    PermissionGrant,
    UserChange,
    ensure_role,
    ensure_user,
    generate_keys,
    grant,
    probe_submit,
    revoke_grants_except,
)
from stolon.frappe.client import Client, SiteConfig
from stolon.frappe.errors import DoesNotExistError, FrappeError, PermissionError_, ValidationError
from stolon.frappe.lifecycle import SubmitNotPermittedError

CONFIG = SiteConfig(url="https://example.test", api_key="KEY", api_secret="SECRET")  # noqa: S106


class RecordingClient:
    """Records every call. `get` raises `DoesNotExistError` for unseeded names."""

    def __init__(self, docs: dict[tuple[str, str], dict[str, Any]] | None = None) -> None:
        self.docs = dict(docs or {})
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.inserts: list[tuple[str, dict[str, Any]]] = []
        self.updates: list[tuple[str, str, dict[str, Any]]] = []

    def get(self, doctype: str, name: str) -> Any:
        key = (doctype, name)
        if key not in self.docs:
            raise DoesNotExistError(f"{doctype} {name} not found")
        return self.docs[key]

    def insert(self, doctype: str, doc: dict[str, Any]) -> Any:
        self.inserts.append((doctype, doc))
        return {"name": doc.get("name", "created")}

    def update(self, doctype: str, name: str, doc: dict[str, Any]) -> Any:
        self.updates.append((doctype, name, doc))
        return {"name": name}

    def call(self, dotted_path: str, params: dict[str, Any] | None = None) -> Any:
        self.calls.append((dotted_path, params or {}))
        return {}


# --- PermissionGrant -------------------------------------------------------


def test_permission_grant_accepts_a_subset_of_ptypes() -> None:
    g = PermissionGrant("Account", frozenset({"read", "write"}))
    assert g.ptypes == frozenset({"read", "write"})


def test_permission_grant_rejects_an_unmanaged_ptype() -> None:
    with pytest.raises(ValueError, match="if_owner"):
        PermissionGrant("Account", frozenset({"if_owner"}))


# --- ensure_role -------------------------------------------------------


def test_ensure_role_creates_when_missing() -> None:
    client = RecordingClient()
    created = ensure_role(client, "Transpire Books Bot")
    assert created is True
    assert client.inserts == [
        ("Role", {"role_name": "Transpire Books Bot", "desk_access": 0}),
    ]


def test_ensure_role_is_a_no_op_when_present() -> None:
    client = RecordingClient(docs={("Role", "Transpire Books Bot"): {"name": "Transpire Books Bot"}})
    created = ensure_role(client, "Transpire Books Bot")
    assert created is False
    assert client.inserts == []


def test_ensure_role_can_grant_desk_access() -> None:
    client = RecordingClient()
    ensure_role(client, "Transpire Books Bot", desk_access=True)
    assert client.inserts == [
        ("Role", {"role_name": "Transpire Books Bot", "desk_access": 1}),
    ]


# --- grant -------------------------------------------------------


def test_grant_writes_all_ptypes_with_explicit_zeros() -> None:
    client = RecordingClient()
    sent = grant(client, "Transpire Books Bot", PermissionGrant("Account", frozenset({"read", "write", "create"})))

    add_calls = [c for c in client.calls if c[0] == f"{_PERMISSION_MANAGER}.add"]
    assert add_calls == [
        (f"{_PERMISSION_MANAGER}.add", {"parent": "Account", "role": "Transpire Books Bot", "permlevel": 0})
    ]

    update_calls = [c for c in client.calls if c[0] == f"{_PERMISSION_MANAGER}.update"]
    sent_ptypes = {c[1]["ptype"]: c[1]["value"] for c in update_calls}
    assert set(sent_ptypes) == PTYPES, "every managed ptype must be sent, not just the granted ones"
    assert sent_ptypes["read"] == "1"
    assert sent_ptypes["write"] == "1"
    assert sent_ptypes["create"] == "1"
    # The point of the function: submit/cancel/amend are explicit zeros, not
    # left unset for Frappe's own default to decide.
    assert sent_ptypes["submit"] == "0"
    assert sent_ptypes["cancel"] == "0"
    assert sent_ptypes["amend"] == "0"
    assert sent_ptypes["delete"] == "0"

    assert set(sent) == set(sent_ptypes.items())


def test_grant_update_calls_name_the_doctype_role_and_permlevel() -> None:
    client = RecordingClient()
    grant(client, "Transpire Books Bot", PermissionGrant("Account", frozenset({"read"})))
    update_calls = [c[1] for c in client.calls if c[0] == f"{_PERMISSION_MANAGER}.update"]
    for params in update_calls:
        assert params["doctype"] == "Account"
        assert params["role"] == "Transpire Books Bot"
        assert params["permlevel"] == 0


# --- revoke_grants_except -------------------------------------------------------


class _RevokeClient(RecordingClient):
    """Answers `get_permissions` with fixed rows; `remove` can be told to raise per doctype."""

    def __init__(self, rows: list[dict[str, Any]], *, raise_on_remove: dict[str, Exception] | None = None) -> None:
        super().__init__()
        self._rows = rows
        self._raise_on_remove = dict(raise_on_remove or {})

    def call(self, dotted_path: str, params: dict[str, Any] | None = None) -> Any:
        self.calls.append((dotted_path, params or {}))
        if dotted_path == f"{_PERMISSION_MANAGER}.get_permissions":
            return self._rows
        if dotted_path == f"{_PERMISSION_MANAGER}.remove":
            doctype = (params or {})["doctype"]
            if doctype in self._raise_on_remove:
                raise self._raise_on_remove[doctype]
            return {}
        raise AssertionError(f"unexpected call: {dotted_path}")


def test_revoke_grants_except_removes_doctypes_outside_keep() -> None:
    client = _RevokeClient([
        {"parent": "Account", "permlevel": 0},
        {"parent": "DocType", "permlevel": 0},
    ])
    revoked = revoke_grants_except(client, "Transpire Books Bot", keep=frozenset({"Account"}))
    assert revoked == ["DocType"]
    remove_calls = [c for c in client.calls if c[0] == f"{_PERMISSION_MANAGER}.remove"]
    assert remove_calls == [
        (f"{_PERMISSION_MANAGER}.remove", {"doctype": "DocType", "role": "Transpire Books Bot", "permlevel": 0})
    ]


def test_revoke_grants_except_asks_get_permissions_for_this_role() -> None:
    client = _RevokeClient([{"parent": "Account", "permlevel": 0}])
    revoke_grants_except(client, "Transpire Books Bot", keep=frozenset({"Account"}))
    get_calls = [c for c in client.calls if c[0] == f"{_PERMISSION_MANAGER}.get_permissions"]
    assert get_calls == [(f"{_PERMISSION_MANAGER}.get_permissions", {"role": "Transpire Books Bot"})]


def test_revoke_grants_except_returns_empty_when_nothing_to_revoke() -> None:
    client = _RevokeClient([{"parent": "Account", "permlevel": 0}, {"parent": "Company", "permlevel": 0}])
    revoked = revoke_grants_except(client, "Transpire Books Bot", keep=frozenset({"Account", "Company"}))
    assert revoked == []
    assert not [c for c in client.calls if c[0] == f"{_PERMISSION_MANAGER}.remove"]


def test_revoke_grants_except_propagates_with_the_doctype_named() -> None:
    # Frappe's own guard against leaving a doctype with zero permission
    # rules -- if this role is the doctype's only rule, remove cannot
    # succeed, and that must surface, not be silently skipped.
    client = _RevokeClient(
        [{"parent": "DocType", "permlevel": 0}],
        raise_on_remove={"DocType": ValidationError("There must be atleast one permission rule.")},
    )
    with pytest.raises(FrappeError, match="DocType"):
        revoke_grants_except(client, "Transpire Books Bot", keep=frozenset())


# --- ensure_user -------------------------------------------------------


def test_ensure_user_creates_when_missing() -> None:
    client = RecordingClient()
    change = ensure_user(
        client, "books-robot@example.com", first_name="Books Bot", roles=frozenset({"Transpire Books Bot"})
    )
    assert change == UserChange(created=True, added_roles=frozenset({"Transpire Books Bot"}), removed_roles=frozenset())
    assert client.inserts == [
        (
            "User",
            {
                "email": "books-robot@example.com",
                "first_name": "Books Bot",
                "user_type": "System User",
                "send_welcome_email": 0,
                "enabled": 1,
                "roles": [{"role": "Transpire Books Bot"}],
            },
        )
    ]


def test_ensure_user_reconciles_roles_to_exactly_the_requested_set() -> None:
    # A stray hand-added role is a privilege escalation riding along on the
    # bot's API key -- it must be removed, not merely supplemented.
    client = RecordingClient(
        docs={
            ("User", "books-robot@example.com"): {
                "name": "books-robot@example.com",
                "roles": [{"role": "Transpire Books Bot"}, {"role": "System Manager"}],
            }
        }
    )
    change = ensure_user(
        client, "books-robot@example.com", first_name="Books Bot", roles=frozenset({"Transpire Books Bot"})
    )
    assert change.created is False
    assert change.added_roles == frozenset()
    assert change.removed_roles == frozenset({"System Manager"})
    assert client.updates == [("User", "books-robot@example.com", {"roles": [{"role": "Transpire Books Bot"}]})]


def test_ensure_user_adds_a_missing_role() -> None:
    client = RecordingClient(
        docs={("User", "books-robot@example.com"): {"name": "books-robot@example.com", "roles": []}}
    )
    change = ensure_user(
        client, "books-robot@example.com", first_name="Books Bot", roles=frozenset({"Transpire Books Bot"})
    )
    assert change.added_roles == frozenset({"Transpire Books Bot"})
    assert change.removed_roles == frozenset()
    assert client.updates == [("User", "books-robot@example.com", {"roles": [{"role": "Transpire Books Bot"}]})]


def test_ensure_user_is_a_no_op_when_roles_already_match() -> None:
    client = RecordingClient(
        docs={
            ("User", "books-robot@example.com"): {
                "name": "books-robot@example.com",
                "roles": [{"role": "Transpire Books Bot"}],
            }
        }
    )
    change = ensure_user(
        client, "books-robot@example.com", first_name="Books Bot", roles=frozenset({"Transpire Books Bot"})
    )
    assert change == UserChange(created=False, added_roles=frozenset(), removed_roles=frozenset())
    assert client.updates == []


# --- generate_keys -------------------------------------------------------


class _KeysClient(RecordingClient):
    """A `RecordingClient` whose `call` answers `generate_keys` with a fixed pair."""

    def __init__(self, api_key: str, api_secret: str) -> None:
        super().__init__()
        self._api_key = api_key
        self._api_secret = api_secret

    def call(self, dotted_path: str, params: dict[str, Any] | None = None) -> Any:
        self.calls.append((dotted_path, params or {}))
        return {"api_key": self._api_key, "api_secret": self._api_secret}


def test_generate_keys_returns_the_pair() -> None:
    client = _KeysClient("newkey", "newsecret")
    keys = generate_keys(client, "books-robot@example.com")
    assert keys.api_key == "newkey"
    assert keys.api_secret == "newsecret"
    assert client.calls == [("frappe.core.doctype.user.user.generate_keys", {"user": "books-robot@example.com"})]


def test_api_key_pair_repr_hides_the_secret() -> None:
    client = _KeysClient("newkey", "newsecret")
    keys = generate_keys(client, "books-robot@example.com")
    rendered = repr(keys)
    assert "newkey" not in rendered
    assert "newsecret" not in rendered


# --- probe_submit -------------------------------------------------------


def _client_that(
    behavior: Any,
    *,
    can_create: bool = True,
    submit_flag: bool = False,
    has_permission_error: Exception | None = None,
    permission_calls: list[dict[str, Any]] | None = None,
) -> type:
    """A fake `Client` whose `insert` runs `behavior(doctype, doc)`.

    `call` answers `frappe.client.has_permission` per `perm_type` with
    `can_create`/`submit_flag`, or raises `has_permission_error` if given --
    the propagation case, where the probe's permission check itself fails.
    Every call's params are appended to `permission_calls`, when given, so a
    test can inspect them without reaching into the constructed instance.
    """

    class _FakeClient:
        def __init__(self, config: SiteConfig) -> None:
            self.config = config

        def __enter__(self) -> _FakeClient:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def call(self, dotted_path: str, params: dict[str, Any] | None = None) -> Any:
            assert dotted_path == "frappe.client.has_permission"
            params = params or {}
            if permission_calls is not None:
                permission_calls.append(params)
            if has_permission_error is not None:
                raise has_permission_error
            answer = {"create": can_create, "submit": submit_flag}[params["perm_type"]]
            return {"has_permission": answer}

        def insert(self, doctype: str, doc: dict[str, Any]) -> Any:
            return behavior(doctype, doc)

    return _FakeClient


def test_probe_lifts_may_submit_because_the_local_guard_would_otherwise_refuse() -> None:
    # Proves the lift in probe_submit is load-bearing: without it, the plain
    # client's own guard refuses this exact document locally, before any
    # request reaches the server -- which would prove nothing about server
    # role permissions, the only thing this probe exists to test.
    with pytest.raises(SubmitNotPermittedError):
        Client(CONFIG).insert(
            "Journal Entry",
            {
                "company": "Acmebike Cycling",
                "posting_date": "2000-01-01",
                "voucher_type": "Journal Entry",
                "docstatus": 1,
            },
        )


def test_probe_submit_constructs_the_client_with_may_submit_lifted(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[SiteConfig] = []

    class _RecordingClient:
        def __init__(self, config: SiteConfig) -> None:
            seen.append(config)
            self.config = config

        def __enter__(self) -> _RecordingClient:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def call(self, dotted_path: str, params: dict[str, Any] | None = None) -> Any:
            return {"has_permission": True}

        def insert(self, doctype: str, doc: dict[str, Any]) -> Any:
            return {"name": "x"}

    monkeypatch.setattr("stolon.frappe.access.Client", _RecordingClient)

    probe_submit(CONFIG, company="Acmebike Cycling")

    assert seen[0].may_submit is True
    assert CONFIG.may_submit is False, "the original config must be untouched"


def test_probe_classifies_a_true_positive_refusal_as_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    # (can_create=True, submit_flag=False, insert raises PermissionError_):
    # the only combination that actually proves the refusal targets submit.
    def raise_permission_error(doctype: str, doc: dict[str, Any]) -> Any:
        raise PermissionError_("No permission for Journal Entry")

    monkeypatch.setattr(
        "stolon.frappe.access.Client",
        _client_that(raise_permission_error, can_create=True, submit_flag=False),
    )
    probe = probe_submit(CONFIG, company="Acmebike Cycling")
    assert probe.refused is True
    assert probe.can_create is True
    assert probe.submit_flag is False
    assert isinstance(probe.error, PermissionError_)
    assert probe.document_name is None


def test_probe_does_not_confuse_a_total_lockout_with_a_submit_scoped_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The false-positive a live run actually hit: (can_create=False,
    # submit_flag=False, insert raises PermissionError_ with the SAME generic
    # message a submit-scoped refusal would produce). A credential locked out
    # of drafting entirely must not read as "correctly scoped to no-submit".
    def raise_permission_error(doctype: str, doc: dict[str, Any]) -> Any:
        raise PermissionError_(
            "User books-robot@example.com does not have doctype access via role permission for document Journal Entry"
        )

    monkeypatch.setattr(
        "stolon.frappe.access.Client",
        _client_that(raise_permission_error, can_create=False, submit_flag=False),
    )
    probe = probe_submit(CONFIG, company="Acmebike Cycling")
    assert probe.refused is False
    assert probe.can_create is False
    assert probe.submit_flag is False
    assert isinstance(probe.error, PermissionError_)


def test_probe_classifies_a_validation_stop_as_not_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    # (can_create=True, submit_flag=True, insert raises ValidationError):
    # permission passed and reached validation, which stopped it -- this
    # proves nothing about the role's submit permission either way, so it is
    # not classified as "refused".
    def raise_validation_error(doctype: str, doc: dict[str, Any]) -> Any:
        raise ValidationError("Accounts is mandatory")

    monkeypatch.setattr(
        "stolon.frappe.access.Client",
        _client_that(raise_validation_error, can_create=True, submit_flag=True),
    )
    probe = probe_submit(CONFIG, company="Acmebike Cycling")
    assert probe.refused is False
    assert probe.can_create is True
    assert probe.submit_flag is True
    assert isinstance(probe.error, ValidationError)
    assert probe.document_name is None


def test_probe_classifies_an_unexpected_success_as_not_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    # (can_create=True, submit_flag=False, insert succeeds): the catastrophic
    # case -- permission passed and the document actually posted despite
    # missing `accounts`.
    def succeed(doctype: str, doc: dict[str, Any]) -> Any:
        return {"name": "ACC-JV-2026-00001"}

    monkeypatch.setattr("stolon.frappe.access.Client", _client_that(succeed, can_create=True, submit_flag=False))
    probe = probe_submit(CONFIG, company="Acmebike Cycling")
    assert probe.refused is False
    assert probe.can_create is True
    assert probe.submit_flag is False
    assert probe.error is None
    assert probe.document_name == "ACC-JV-2026-00001"


def test_probe_propagates_a_failed_has_permission_call(monkeypatch: pytest.MonkeyPatch) -> None:
    # The probe's own permission check failing is not an answer about
    # permissions -- it means the probe could not run at all, like a
    # transport error, so it must propagate rather than becoming a SubmitProbe.
    def unreachable(doctype: str, doc: dict[str, Any]) -> Any:
        raise AssertionError("insert must not run when has_permission itself failed")

    monkeypatch.setattr(
        "stolon.frappe.access.Client",
        _client_that(unreachable, has_permission_error=PermissionError_("no permission to check permissions")),
    )
    with pytest.raises(PermissionError_):
        probe_submit(CONFIG, company="Acmebike Cycling")


def test_probe_asks_has_permission_for_create_and_submit_with_no_docname(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def raise_permission_error(doctype: str, doc: dict[str, Any]) -> Any:
        raise PermissionError_("no permission")

    monkeypatch.setattr(
        "stolon.frappe.access.Client",
        _client_that(raise_permission_error, permission_calls=calls),
    )
    probe_submit(CONFIG, company="Acmebike Cycling")
    assert {c["perm_type"] for c in calls} == {"create", "submit"}
    assert all(c["doctype"] == "Journal Entry" for c in calls)
    assert all(c["docname"] == "" for c in calls)


def test_probe_submit_sends_a_document_missing_mandatory_accounts(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []

    def record(doctype: str, doc: dict[str, Any]) -> Any:
        seen.append(doc)
        raise PermissionError_("No permission")

    monkeypatch.setattr("stolon.frappe.access.Client", _client_that(record))
    probe_submit(CONFIG, company="Acmebike Cycling")
    assert seen == [
        {
            "company": "Acmebike Cycling",
            "posting_date": "2000-01-01",
            "voucher_type": "Journal Entry",
            "docstatus": 1,
        }
    ]
    assert "accounts" not in seen[0]
