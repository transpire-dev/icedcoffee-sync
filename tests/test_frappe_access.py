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

from icedcoffee_sync.frappe.access import (
    _ALWAYS_RESET_TO_ZERO,
    _PERMISSION_MANAGER,
    PTYPES,
    OrphanProbeDocumentError,
    PermissionGrant,
    RoleChange,
    UserChange,
    ensure_role,
    ensure_user,
    generate_keys,
    grant,
    granted_flags_effective,
    probe_submit,
    prohibited_flags_allowed,
    revoke_grants_except,
)
from icedcoffee_sync.frappe.client import Client, SiteConfig
from icedcoffee_sync.frappe.errors import DoesNotExistError, FrappeError, PermissionError_, ValidationError
from icedcoffee_sync.frappe.lifecycle import SubmitNotPermittedError

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


def test_permission_grant_defaults_to_permlevel_zero() -> None:
    g = PermissionGrant("Account", frozenset({"read"}))
    assert g.permlevel == 0


def test_permission_grant_rejects_a_negative_permlevel() -> None:
    with pytest.raises(ValueError, match="permlevel"):
        PermissionGrant("Account", frozenset({"read"}), permlevel=-1)


# --- ensure_role -------------------------------------------------------


def test_ensure_role_creates_when_missing() -> None:
    client = RecordingClient()
    change = ensure_role(client, "Transpire Books Bot")
    assert change == RoleChange(created=True, desk_access_repaired=False)
    assert client.inserts == [
        ("Role", {"role_name": "Transpire Books Bot", "desk_access": 0}),
    ]


def test_ensure_role_is_a_no_op_when_present_and_desk_access_matches() -> None:
    client = RecordingClient(docs={("Role", "Transpire Books Bot"): {"name": "Transpire Books Bot", "desk_access": 0}})
    change = ensure_role(client, "Transpire Books Bot")
    assert change == RoleChange(created=False, desk_access_repaired=False)
    assert client.inserts == []
    assert client.updates == []


def test_ensure_role_can_grant_desk_access() -> None:
    client = RecordingClient()
    ensure_role(client, "Transpire Books Bot", desk_access=True)
    assert client.inserts == [
        ("Role", {"role_name": "Transpire Books Bot", "desk_access": 1}),
    ]


def test_ensure_role_repairs_desk_access_when_it_differs() -> None:
    client = RecordingClient(docs={("Role", "Transpire Books Bot"): {"name": "Transpire Books Bot", "desk_access": 1}})
    change = ensure_role(client, "Transpire Books Bot", desk_access=False)
    assert change == RoleChange(created=False, desk_access_repaired=True)
    assert client.updates == [("Role", "Transpire Books Bot", {"desk_access": 0})]


def test_ensure_role_repairs_desk_access_the_other_direction() -> None:
    client = RecordingClient(docs={("Role", "Transpire Books Bot"): {"name": "Transpire Books Bot", "desk_access": 0}})
    change = ensure_role(client, "Transpire Books Bot", desk_access=True)
    assert change == RoleChange(created=False, desk_access_repaired=True)
    assert client.updates == [("Role", "Transpire Books Bot", {"desk_access": 1})]


def test_ensure_role_treats_a_missing_desk_access_field_as_falsy() -> None:
    # A role read back with no desk_access key at all (rather than an
    client = RecordingClient(docs={("Role", "Transpire Books Bot"): {"name": "Transpire Books Bot"}})
    change = ensure_role(client, "Transpire Books Bot")
    assert change == RoleChange(created=False, desk_access_repaired=False)
    assert client.updates == []


def test_ensure_role_re_enables_a_disabled_role() -> None:
    client = RecordingClient(
        docs={("Role", "Transpire Books Bot"): {"name": "Transpire Books Bot", "desk_access": 0, "disabled": 1}}
    )
    change = ensure_role(client, "Transpire Books Bot")
    assert change == RoleChange(created=False, desk_access_repaired=False, disabled_repaired=True)
    assert client.updates == [("Role", "Transpire Books Bot", {"disabled": 0})]


def test_ensure_role_is_a_no_op_when_present_enabled_and_desk_access_matches() -> None:
    client = RecordingClient(
        docs={("Role", "Transpire Books Bot"): {"name": "Transpire Books Bot", "desk_access": 0, "disabled": 0}}
    )
    change = ensure_role(client, "Transpire Books Bot")
    assert change == RoleChange(created=False, desk_access_repaired=False, disabled_repaired=False)
    assert client.updates == []


def test_ensure_role_repairs_desk_access_and_disabled_in_the_same_update() -> None:
    # Both repairs must land in one write, not two -- the same way
    client = RecordingClient(
        docs={("Role", "Transpire Books Bot"): {"name": "Transpire Books Bot", "desk_access": 1, "disabled": 1}}
    )
    change = ensure_role(client, "Transpire Books Bot", desk_access=False)
    assert change == RoleChange(created=False, desk_access_repaired=True, disabled_repaired=True)
    assert client.updates == [("Role", "Transpire Books Bot", {"desk_access": 0, "disabled": 0})]


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
    assert set(sent_ptypes) == PTYPES | _ALWAYS_RESET_TO_ZERO, (
        "every managed ptype must be sent, not just the granted ones"
    )
    assert sent_ptypes["read"] == "1"
    assert sent_ptypes["write"] == "1"
    assert sent_ptypes["create"] == "1"
    # The point of the function: submit/cancel/amend are explicit zeros, not
    assert sent_ptypes["submit"] == "0"
    assert sent_ptypes["cancel"] == "0"
    assert sent_ptypes["amend"] == "0"
    assert sent_ptypes["delete"] == "0"

    assert set(sent) == set(sent_ptypes.items())


def test_grant_always_resets_if_owner_and_select_to_zero() -> None:
    client = RecordingClient()
    grant(client, "Transpire Books Bot", PermissionGrant("Account", frozenset({"read"})))
    update_calls = [c[1] for c in client.calls if c[0] == f"{_PERMISSION_MANAGER}.update"]
    sent_ptypes = {c["ptype"]: c["value"] for c in update_calls}
    assert sent_ptypes["if_owner"] == "0"
    assert sent_ptypes["select"] == "0"


def test_permission_grant_still_rejects_if_owner_and_select() -> None:
    # These are reset unconditionally by `grant`, never something a caller
    with pytest.raises(ValueError, match="if_owner"):
        PermissionGrant("Account", frozenset({"if_owner"}))
    with pytest.raises(ValueError, match="select"):
        PermissionGrant("Account", frozenset({"select"}))


def test_grant_update_calls_name_the_doctype_role_and_permlevel() -> None:
    client = RecordingClient()
    grant(client, "Transpire Books Bot", PermissionGrant("Account", frozenset({"read"})))
    update_calls = [c[1] for c in client.calls if c[0] == f"{_PERMISSION_MANAGER}.update"]
    for params in update_calls:
        assert params["doctype"] == "Account"
        assert params["role"] == "Transpire Books Bot"
        assert params["permlevel"] == 0


def test_grant_sends_a_nonzero_permlevel_on_the_add_and_every_update_call() -> None:
    client = RecordingClient()
    grant(client, "Transpire Books Bot", PermissionGrant("Company", frozenset({"read"}), permlevel=1))

    add_calls = [c for c in client.calls if c[0] == f"{_PERMISSION_MANAGER}.add"]
    assert add_calls == [
        (f"{_PERMISSION_MANAGER}.add", {"parent": "Company", "role": "Transpire Books Bot", "permlevel": 1})
    ]

    update_calls = [c[1] for c in client.calls if c[0] == f"{_PERMISSION_MANAGER}.update"]
    assert update_calls, "grant must still send every managed ptype"
    for params in update_calls:
        assert params["permlevel"] == 1


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
    revoked = revoke_grants_except(client, "Transpire Books Bot", keep=frozenset({("Account", 0)}))
    assert revoked == ["DocType"]
    remove_calls = [c for c in client.calls if c[0] == f"{_PERMISSION_MANAGER}.remove"]
    assert remove_calls == [
        (f"{_PERMISSION_MANAGER}.remove", {"doctype": "DocType", "role": "Transpire Books Bot", "permlevel": 0})
    ]


def test_revoke_grants_except_removes_a_nonzero_permlevel_row_on_an_allowed_doctype() -> None:
    client = _RevokeClient([
        {"parent": "Account", "permlevel": 0},
        {"parent": "Account", "permlevel": 1},
    ])
    revoked = revoke_grants_except(client, "Transpire Books Bot", keep=frozenset({("Account", 0)}))
    assert revoked == ["Account (permlevel 1)"]
    remove_calls = [c for c in client.calls if c[0] == f"{_PERMISSION_MANAGER}.remove"]
    assert remove_calls == [
        (f"{_PERMISSION_MANAGER}.remove", {"doctype": "Account", "role": "Transpire Books Bot", "permlevel": 1})
    ]


def test_revoke_grants_except_keeps_a_granted_nonzero_permlevel_row() -> None:
    # The point of this task: a role granted Company at permlevel 1 must
    # survive the next provision run, not be revoked alongside every other
    # nonzero-permlevel row.
    client = _RevokeClient([
        {"parent": "Company", "permlevel": 0},
        {"parent": "Company", "permlevel": 1},
        {"parent": "Account", "permlevel": 1},
    ])
    revoked = revoke_grants_except(client, "Transpire Books Bot", keep=frozenset({("Company", 0), ("Company", 1)}))
    assert revoked == ["Account (permlevel 1)"]
    remove_calls = [c for c in client.calls if c[0] == f"{_PERMISSION_MANAGER}.remove"]
    assert remove_calls == [
        (f"{_PERMISSION_MANAGER}.remove", {"doctype": "Account", "role": "Transpire Books Bot", "permlevel": 1})
    ]


def test_revoke_grants_except_asks_get_permissions_for_this_role() -> None:
    client = _RevokeClient([{"parent": "Account", "permlevel": 0}])
    revoke_grants_except(client, "Transpire Books Bot", keep=frozenset({("Account", 0)}))
    get_calls = [c for c in client.calls if c[0] == f"{_PERMISSION_MANAGER}.get_permissions"]
    assert get_calls == [(f"{_PERMISSION_MANAGER}.get_permissions", {"role": "Transpire Books Bot"})]


def test_revoke_grants_except_returns_empty_when_nothing_to_revoke() -> None:
    client = _RevokeClient([{"parent": "Account", "permlevel": 0}, {"parent": "Company", "permlevel": 0}])
    revoked = revoke_grants_except(client, "Transpire Books Bot", keep=frozenset({("Account", 0), ("Company", 0)}))
    assert revoked == []
    assert not [c for c in client.calls if c[0] == f"{_PERMISSION_MANAGER}.remove"]


def test_revoke_grants_except_propagates_with_the_doctype_named() -> None:
    # Frappe's own guard against leaving a doctype with zero permission
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


def test_ensure_user_re_enables_a_disabled_existing_user() -> None:
    client = RecordingClient(
        docs={
            ("User", "books-robot@example.com"): {
                "name": "books-robot@example.com",
                "roles": [{"role": "Transpire Books Bot"}],
                "enabled": 0,
            }
        }
    )
    change = ensure_user(
        client, "books-robot@example.com", first_name="Books Bot", roles=frozenset({"Transpire Books Bot"})
    )
    assert change.enabled_repaired is True
    assert client.updates == [("User", "books-robot@example.com", {"enabled": 1})]


def test_ensure_user_combines_role_and_enable_repair_into_one_update() -> None:
    client = RecordingClient(
        docs={
            ("User", "books-robot@example.com"): {
                "name": "books-robot@example.com",
                "roles": [],
                "enabled": 0,
            }
        }
    )
    change = ensure_user(
        client, "books-robot@example.com", first_name="Books Bot", roles=frozenset({"Transpire Books Bot"})
    )
    assert change.added_roles == frozenset({"Transpire Books Bot"})
    assert change.enabled_repaired is True
    assert client.updates == [
        ("User", "books-robot@example.com", {"roles": [{"role": "Transpire Books Bot"}], "enabled": 1})
    ]


def test_ensure_user_does_not_repair_an_enabled_user() -> None:
    client = RecordingClient(
        docs={
            ("User", "books-robot@example.com"): {
                "name": "books-robot@example.com",
                "roles": [{"role": "Transpire Books Bot"}],
                "enabled": 1,
            }
        }
    )
    change = ensure_user(
        client, "books-robot@example.com", first_name="Books Bot", roles=frozenset({"Transpire Books Bot"})
    )
    assert change.enabled_repaired is False
    assert client.updates == []
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


# --- prohibited_flags_allowed -------------------------------------------------------


class _HasPermissionClient:
    """Answers `frappe.client.has_permission` from a fixed {(doctype, perm_type): bool} map."""

    def __init__(self, answers: dict[tuple[str, str], bool]) -> None:
        self._answers = answers
        self.calls: list[dict[str, Any]] = []

    def __enter__(self) -> _HasPermissionClient:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def call(self, dotted_path: str, params: dict[str, Any] | None = None) -> Any:
        assert dotted_path == "frappe.client.has_permission"
        params = params or {}
        self.calls.append(params)
        return {"has_permission": self._answers[(params["doctype"], params["perm_type"])]}


def test_prohibited_flags_allowed_reports_each_ptype(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _HasPermissionClient({
        ("Purchase Invoice", "submit"): False,
        ("Purchase Invoice", "cancel"): False,
        ("Purchase Invoice", "amend"): True,
    })
    monkeypatch.setattr("icedcoffee_sync.frappe.access.Client", lambda config: client)
    result = prohibited_flags_allowed(
        CONFIG, doctype="Purchase Invoice", ptypes=frozenset({"submit", "cancel", "amend"})
    )
    assert result == {"submit": False, "cancel": False, "amend": True}


def test_prohibited_flags_allowed_asks_with_no_docname(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _HasPermissionClient({("Bank Transaction", "submit"): False})
    monkeypatch.setattr("icedcoffee_sync.frappe.access.Client", lambda config: client)
    prohibited_flags_allowed(CONFIG, doctype="Bank Transaction", ptypes=frozenset({"submit"}))
    assert client.calls == [{"doctype": "Bank Transaction", "docname": "", "perm_type": "submit"}]


# --- granted_flags_effective -------------------------------------------------------


def test_granted_flags_effective_reports_each_ptype(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _HasPermissionClient({
        ("Account", "read"): True,
        ("Account", "write"): False,
        ("Account", "create"): True,
        ("Account", "delete"): True,
    })
    monkeypatch.setattr("icedcoffee_sync.frappe.access.Client", lambda config: client)
    result = granted_flags_effective(CONFIG, doctype="Account", ptypes=frozenset({"read", "write", "create", "delete"}))
    assert result == {"read": True, "write": False, "create": True, "delete": True}


def test_granted_flags_effective_asks_with_no_docname(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _HasPermissionClient({("Company", "write"): True})
    monkeypatch.setattr("icedcoffee_sync.frappe.access.Client", lambda config: client)
    granted_flags_effective(CONFIG, doctype="Company", ptypes=frozenset({"write"}))
    assert client.calls == [{"doctype": "Company", "docname": "", "perm_type": "write"}]


# --- probe_submit -------------------------------------------------------


def _default_draft_behavior(doctype: str, doc: dict[str, Any]) -> Any:
    """The default response to the draft (no-`docstatus`) insert `_probe_create` makes.

    Succeeds with a fake name, which `_probe_create` then deletes -- the
    common case, "create genuinely works", so a test that isn't about
    that step specifically doesn't have to say so.
    """
    return {"name": "DRAFT-JV-0001"}


class _FakeSubmitClient:
    """A fake `Client` whose `insert` runs `behavior` or `draft_behavior`.

    A real (module-level) class rather than one built inside `_client_that`,
    so its methods' branches count toward its own complexity, not the
    factory function's. `behavior(doctype, doc)` drives the submit-flagged
    insert (`docstatus` present); `draft_behavior(doctype, doc)` drives
    `_probe_create`'s preceding draft insert (no `docstatus`), which every
    test now goes through first. `call` answers `frappe.client.has_permission`
    with `submit_flag`, or raises `has_permission_error` if given -- the
    propagation case, where the probe's permission check itself fails.
    Every `has_permission` call's params are appended to `permission_calls`,
    when given, so a test can inspect them without reaching into the
    constructed instance; every `delete` call's (doctype, name) is appended
    to `deleted` the same way.
    """

    def __init__(
        self,
        config: SiteConfig,
        *,
        behavior: Any,
        draft_behavior: Any,
        submit_flag: bool,
        has_permission_error: Exception | None,
        permission_calls: list[dict[str, Any]] | None,
        deleted: list[tuple[str, str]] | None,
        delete_error: Exception | None = None,
    ) -> None:
        self.config = config
        self._behavior = behavior
        self._draft_behavior = draft_behavior
        self._submit_flag = submit_flag
        self._has_permission_error = has_permission_error
        self._permission_calls = permission_calls
        self._deleted = deleted
        self._delete_error = delete_error

    def __enter__(self) -> _FakeSubmitClient:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def call(self, dotted_path: str, params: dict[str, Any] | None = None) -> Any:
        assert dotted_path == "frappe.client.has_permission"
        params = params or {}
        if self._permission_calls is not None:
            self._permission_calls.append(params)
        if self._has_permission_error is not None:
            raise self._has_permission_error
        assert params["perm_type"] == "submit"
        return {"has_permission": self._submit_flag}

    def insert(self, doctype: str, doc: dict[str, Any]) -> Any:
        if "docstatus" in doc:
            return self._behavior(doctype, doc)
        return self._draft_behavior(doctype, doc)

    def delete(self, doctype: str, name: str) -> Any:
        if self._delete_error is not None:
            raise self._delete_error
        if self._deleted is not None:
            self._deleted.append((doctype, name))
        return {}


def _client_that(
    behavior: Any,
    *,
    can_create: bool = True,
    submit_flag: bool = False,
    has_permission_error: Exception | None = None,
    permission_calls: list[dict[str, Any]] | None = None,
    draft_behavior: Any = _default_draft_behavior,
    deleted: list[tuple[str, str]] | None = None,
    delete_error: Exception | None = None,
) -> Any:
    """A factory for `_FakeSubmitClient`, matching `Client`'s single-argument constructor.

    `can_create` is accepted but unused: `probe_submit` no longer asks
    `has_permission` for "create" at all (see its own docstring), so
    nothing in `_FakeSubmitClient` needs to answer that question -- kept
    as a parameter only so existing call sites that still pass it do not
    need to change.
    """
    del can_create

    def factory(config: SiteConfig) -> _FakeSubmitClient:
        return _FakeSubmitClient(
            config,
            behavior=behavior,
            draft_behavior=draft_behavior,
            submit_flag=submit_flag,
            has_permission_error=has_permission_error,
            permission_calls=permission_calls,
            deleted=deleted,
            delete_error=delete_error,
        )

    return factory


def test_probe_lifts_may_submit_because_the_local_guard_would_otherwise_refuse() -> None:
    # Proves the lift in probe_submit is load-bearing: without it, the plain
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

        def delete(self, doctype: str, name: str) -> Any:
            return {}

    monkeypatch.setattr("icedcoffee_sync.frappe.access.Client", _RecordingClient)

    probe_submit(CONFIG, company="Acmebike Cycling")

    assert seen[0].may_submit is True
    assert CONFIG.may_submit is False, "the original config must be untouched"


def test_probe_classifies_a_true_positive_refusal_as_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    # (can_create=True, submit_flag=False, insert raises PermissionError_):
    def raise_permission_error(doctype: str, doc: dict[str, Any]) -> Any:
        raise PermissionError_("No permission for Journal Entry")

    monkeypatch.setattr(
        "icedcoffee_sync.frappe.access.Client",
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
    def raise_permission_error(doctype: str, doc: dict[str, Any]) -> Any:
        raise PermissionError_(
            "User books-robot@example.com does not have doctype access via role permission for document Journal Entry"
        )

    monkeypatch.setattr(
        "icedcoffee_sync.frappe.access.Client",
        # A total lockout means the draft insert `_probe_create` makes is
        _client_that(
            raise_permission_error, can_create=False, submit_flag=False, draft_behavior=raise_permission_error
        ),
    )
    probe = probe_submit(CONFIG, company="Acmebike Cycling")
    assert probe.refused is False
    assert probe.can_create is False
    assert probe.submit_flag is False
    assert isinstance(probe.error, PermissionError_)


def test_probe_does_not_trust_a_doctype_level_create_yes_over_a_document_level_no(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The gap `has_permission` alone cannot see: it is a doctype-level
    def raise_permission_error(doctype: str, doc: dict[str, Any]) -> Any:
        raise PermissionError_("User books-robot@example.com does not have doctype access via role permission")

    def unreachable(doctype: str, doc: dict[str, Any]) -> Any:
        raise AssertionError("the submit-flagged insert must not run when the draft was refused")

    monkeypatch.setattr(
        "icedcoffee_sync.frappe.access.Client",
        _client_that(unreachable, can_create=True, submit_flag=False, draft_behavior=raise_permission_error),
    )
    probe = probe_submit(CONFIG, company="Acmebike Cycling")
    assert probe.refused is False
    assert probe.can_create is False
    assert probe.submit_flag is False
    assert isinstance(probe.error, PermissionError_)


def test_probe_deletes_the_draft_once_create_access_is_confirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    # `_probe_create`'s draft is real evidence, not a dry run -- Frappe has
    def raise_permission_error(doctype: str, doc: dict[str, Any]) -> Any:
        raise PermissionError_("no permission")

    def succeed_with_a_name(doctype: str, doc: dict[str, Any]) -> Any:
        return {"name": "DRAFT-JV-9999"}

    deleted: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "icedcoffee_sync.frappe.access.Client",
        _client_that(raise_permission_error, draft_behavior=succeed_with_a_name, deleted=deleted),
    )
    probe_submit(CONFIG, company="Acmebike Cycling")
    assert deleted == [("Journal Entry", "DRAFT-JV-9999")]


def test_probe_create_raises_a_labeled_error_when_cleanup_cannot_delete_the_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_permission_error(doctype: str, doc: dict[str, Any]) -> Any:
        raise PermissionError_("no permission")

    def succeed_with_a_name(doctype: str, doc: dict[str, Any]) -> Any:
        return {"name": "DRAFT-JV-9999"}

    monkeypatch.setattr(
        "icedcoffee_sync.frappe.access.Client",
        _client_that(
            raise_permission_error,
            draft_behavior=succeed_with_a_name,
            delete_error=PermissionError_("no delete permission"),
        ),
    )
    with pytest.raises(OrphanProbeDocumentError) as exc_info:
        probe_submit(CONFIG, company="Acmebike Cycling")
    assert exc_info.value.doctype == "Journal Entry"
    assert exc_info.value.name == "DRAFT-JV-9999"


def test_probe_create_confirms_access_on_validation_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # The expected way a draft insert proves create access without
    def raise_validation_error(doctype: str, doc: dict[str, Any]) -> Any:
        raise ValidationError("Accounts is mandatory")

    def raise_permission_error(doctype: str, doc: dict[str, Any]) -> Any:
        raise PermissionError_("no permission")

    monkeypatch.setattr(
        "icedcoffee_sync.frappe.access.Client",
        _client_that(raise_permission_error, draft_behavior=raise_validation_error),
    )
    probe = probe_submit(CONFIG, company="Acmebike Cycling")
    # can_create is True (the draft's ValidationError confirmed it), and
    assert probe.can_create is True


def test_probe_create_propagates_a_rate_limit_rather_than_confirming_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from icedcoffee_sync.frappe.errors import RateLimitError

    def raise_rate_limit(doctype: str, doc: dict[str, Any]) -> Any:
        raise RateLimitError("slow down")

    def unreachable(doctype: str, doc: dict[str, Any]) -> Any:
        raise AssertionError("the submit-flagged insert must not run when the draft's outcome is inconclusive")

    monkeypatch.setattr(
        "icedcoffee_sync.frappe.access.Client",
        _client_that(unreachable, draft_behavior=raise_rate_limit),
    )
    with pytest.raises(RateLimitError):
        probe_submit(CONFIG, company="Acmebike Cycling")


def test_probe_classifies_a_validation_stop_as_not_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    # (can_create=True, submit_flag=True, insert raises ValidationError):
    def raise_validation_error(doctype: str, doc: dict[str, Any]) -> Any:
        raise ValidationError("Accounts is mandatory")

    monkeypatch.setattr(
        "icedcoffee_sync.frappe.access.Client",
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
    def succeed(doctype: str, doc: dict[str, Any]) -> Any:
        return {"name": "ACC-JV-2026-00001"}

    monkeypatch.setattr(
        "icedcoffee_sync.frappe.access.Client", _client_that(succeed, can_create=True, submit_flag=False)
    )
    probe = probe_submit(CONFIG, company="Acmebike Cycling")
    assert probe.refused is False
    assert probe.can_create is True
    assert probe.submit_flag is False
    assert probe.error is None
    assert probe.document_name == "ACC-JV-2026-00001"


def test_probe_propagates_a_failed_has_permission_call(monkeypatch: pytest.MonkeyPatch) -> None:
    # The probe's own permission check failing is not an answer about
    def unreachable(doctype: str, doc: dict[str, Any]) -> Any:
        raise AssertionError("insert must not run when has_permission itself failed")

    monkeypatch.setattr(
        "icedcoffee_sync.frappe.access.Client",
        _client_that(unreachable, has_permission_error=PermissionError_("no permission to check permissions")),
    )
    with pytest.raises(PermissionError_):
        probe_submit(CONFIG, company="Acmebike Cycling")


def test_probe_asks_has_permission_for_submit_with_no_docname(monkeypatch: pytest.MonkeyPatch) -> None:
    # No equivalent "create" ask: has_permission is doctype-level and
    calls: list[dict[str, Any]] = []

    def raise_permission_error(doctype: str, doc: dict[str, Any]) -> Any:
        raise PermissionError_("no permission")

    monkeypatch.setattr(
        "icedcoffee_sync.frappe.access.Client",
        _client_that(raise_permission_error, permission_calls=calls),
    )
    probe_submit(CONFIG, company="Acmebike Cycling")
    assert {c["perm_type"] for c in calls} == {"submit"}
    assert all(c["doctype"] == "Journal Entry" for c in calls)
    assert all(c["docname"] == "" for c in calls)


def test_probe_submit_sends_a_document_missing_mandatory_accounts(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []

    def record(doctype: str, doc: dict[str, Any]) -> Any:
        seen.append(doc)
        raise PermissionError_("No permission")

    monkeypatch.setattr("icedcoffee_sync.frappe.access.Client", _client_that(record))
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
