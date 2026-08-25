"""Provision Frappe roles, permissions, and users -- generically.

"Machines draft, humans submit" is a rule this repo wants enforced, but the
enforcement point is not client code: `Client`'s local guard
(`stolon.frappe.client.SiteConfig.may_submit`) is a courtesy that any caller
constructing its own `SiteConfig` can bypass. The only enforcement that
actually holds is the API key's role permissions on the server -- a submit
attempt the server itself refuses with `PermissionError`. This module builds
that: a role whose DocPerm flags never include submit/cancel/amend, a bot
user holding only that role, and a probe that proves the refusal is real by
attempting a submit and checking how the server answers.

Nothing here knows about Transpire, ERPNext, or the books ledger -- callers
supply the doctypes, ptypes, and role/user names as policy.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Protocol

from stolon.frappe.client import Client, SiteConfig
from stolon.frappe.errors import DoesNotExistError, FrappeError, PermissionError_

# The Custom DocPerm flags this module manages explicitly. `if_owner` and
# `select` exist on the doctype too but are not part of any grant this module
# writes -- if a caller ever needs them, they belong in a deliberate addition
# here, not a silent gap in PTYPES that `grant` never touches.
#
# Confirmed against `Custom DocPerm`'s Check-type fields
# (frappe/core/doctype/custom_docperm/custom_docperm.json on the Frappe
# `version-15` branch): if_owner, read, write, create, delete, submit,
# cancel, amend, report, export, import, share, print, email, select.
PTYPES: frozenset[str] = frozenset({
    "read",
    "write",
    "create",
    "delete",
    "submit",
    "cancel",
    "amend",
    "report",
    "export",
    "import",
    "print",
    "email",
    "share",
})

# The whitelisted permission-manager methods this module drives, each
# `frappe.only_for("System Manager")` on the server
# (frappe/core/page/permission_manager/permission_manager.py, `version-15`).
_PERMISSION_MANAGER = "frappe.core.page.permission_manager.permission_manager"

# Frappe's own whitelisted method for (re)issuing an API key pair
# (frappe/core/doctype/user/user.py, `version-15`): keeps any existing
# api_key, rotates api_secret every call. Caller needs System Manager.
_GENERATE_KEYS = "frappe.core.doctype.user.user.generate_keys"

# A probe document deliberately missing the mandatory `accounts` child table:
# if permission were ever to pass, Frappe's own mandatory-field validation
# stops it from posting anything.
_PROBE_DOCTYPE = "Journal Entry"


@dataclasses.dataclass(frozen=True)
class PermissionGrant:
    """One doctype's permission flags for a role.

    `ptypes` names only the flags to turn ON; `grant` still sends every
    other managed `PTYPES` member as an explicit "0" -- the absence of
    `submit` here is what makes the role unable to submit, not a hope that
    Frappe's default happens to agree.
    """

    doctype: str
    ptypes: frozenset[str]

    def __post_init__(self) -> None:
        unknown = sorted(self.ptypes - PTYPES)
        if unknown:
            raise ValueError(f"not a managed permission type: {', '.join(unknown)}; PTYPES is {sorted(PTYPES)}")


class _AccessClient(Protocol):
    """The slice of `Client` this module needs. Keeps fakes honest in tests."""

    def get(self, doctype: str, name: str) -> Any: ...
    def insert(self, doctype: str, doc: dict[str, Any]) -> Any: ...
    def update(self, doctype: str, name: str, doc: dict[str, Any]) -> Any: ...
    def call(self, dotted_path: str, params: dict[str, Any] | None = None) -> Any: ...


def ensure_role(client: _AccessClient, role_name: str, *, desk_access: bool = False) -> bool:
    """Create `role_name` if it does not already exist.

    Args:
        client: A Frappe client with System Manager permissions.
        role_name: The role's name, which is also its `role_name` field --
            Role auto-names from that field.
        desk_access: Whether the role can log into the desk UI at all. False
            for a pure API-key bot: it never opens a browser session.

    Returns:
        True if the role was created, False if it already existed.
    """
    try:
        client.get("Role", role_name)
    except DoesNotExistError:
        client.insert("Role", {"role_name": role_name, "desk_access": 1 if desk_access else 0})
        return True
    return False


def grant(client: _AccessClient, role: str, permission_grant: PermissionGrant) -> list[tuple[str, str]]:
    """Set every managed ptype for `role` on `permission_grant.doctype`, explicitly.

    Ensures the (doctype, role, permlevel 0) Custom DocPerm row exists via the
    permission manager's `add`, which is a no-op (not an error) when the row
    is already there -- confirmed against `frappe/permissions.py::add_permission`
    on the Frappe `version-15` branch. Then calls `update` once per member of
    `PTYPES`, sending "1" for anything in `permission_grant.ptypes` and "0"
    for everything else. The explicit zeros are the point: a role granted
    read/write/create with submit left unmentioned would depend on a default
    this module does not control, rather than asserting "no submit" every
    time it runs.

    Returns:
        The (ptype, value) pairs sent, one per member of `PTYPES`.
    """
    client.call(
        f"{_PERMISSION_MANAGER}.add",
        {"parent": permission_grant.doctype, "role": role, "permlevel": 0},
    )
    sent: list[tuple[str, str]] = []
    for ptype in sorted(PTYPES):
        value = "1" if ptype in permission_grant.ptypes else "0"
        client.call(
            f"{_PERMISSION_MANAGER}.update",
            {"doctype": permission_grant.doctype, "role": role, "permlevel": 0, "ptype": ptype, "value": value},
        )
        sent.append((ptype, value))
    return sent


@dataclasses.dataclass(frozen=True)
class UserChange:
    """What `ensure_user` did to reach the requested role set."""

    created: bool
    added_roles: frozenset[str]
    removed_roles: frozenset[str]


def ensure_user(client: _AccessClient, email: str, *, first_name: str, roles: frozenset[str]) -> UserChange:
    """Create the user if missing, or reconcile its roles to exactly `roles`.

    A new user is created with `send_welcome_email: 0` (nothing to welcome --
    it never logs in interactively), `user_type: "System User"`, and
    `enabled: 1`. Field names confirmed against
    `frappe/core/doctype/user/user.json` on the Frappe `version-15` branch;
    the roles child table (`Has Role`, fieldname `roles`) rows carry a single
    `role` field.

    An existing user's role set is made to match `roles` exactly, not merged
    with it: a role added by hand since the last run is a privilege
    escalation riding along on this user's API key, and removing it is the
    correct outcome, not collateral damage.

    Returns:
        A `UserChange` recording whether the user was created, and which
        roles were added or removed to reach `roles`.
    """
    try:
        existing = client.get("User", email)
    except DoesNotExistError:
        client.insert(
            "User",
            {
                "email": email,
                "first_name": first_name,
                "user_type": "System User",
                "send_welcome_email": 0,
                "enabled": 1,
                "roles": [{"role": role} for role in sorted(roles)],
            },
        )
        return UserChange(created=True, added_roles=frozenset(roles), removed_roles=frozenset())

    current = frozenset(row["role"] for row in existing.get("roles", []))
    added = roles - current
    removed = current - roles
    if added or removed:
        client.update("User", email, {"roles": [{"role": role} for role in sorted(roles)]})
    return UserChange(created=False, added_roles=added, removed_roles=removed)


@dataclasses.dataclass(frozen=True)
class ApiKeyPair:
    """A Frappe API key and secret. Kept out of reprs, like `SiteConfig`."""

    api_key: str = dataclasses.field(repr=False)
    api_secret: str = dataclasses.field(repr=False)


def generate_keys(client: _AccessClient, user: str) -> ApiKeyPair:
    """Issue a fresh API secret for `user`, keeping any existing api_key.

    Calls Frappe's whitelisted `generate_keys` method. Caller needs System
    Manager. Rotates `api_secret` on every call -- there is no way to read
    back a previously issued secret, only to mint a new one.
    """
    result = client.call(_GENERATE_KEYS, {"user": user})
    return ApiKeyPair(api_key=result["api_key"], api_secret=result["api_secret"])


@dataclasses.dataclass(frozen=True)
class SubmitProbe:
    """The outcome of attempting a submit-on-insert with a real credential.

    `refused=True` is the property this whole module exists to prove: the
    server's role permissions stopped the docstatus transition
    (`Document.check_docstatus_transition`, called from `check_if_latest()`)
    before any validation ran, and it came back as `PermissionError`.

    `refused=False` with `error` set means permission passed and Frappe's own
    mandatory-field validation stopped it instead -- proof of nothing about
    submit permission, since validation never got the chance to matter.

    `refused=False` with `document_name` set is the catastrophic case:
    permission passed, and despite the probe document deliberately omitting
    the mandatory `accounts` rows, something actually posted. Any caller
    getting this back should treat it as an incident, not a retry.
    """

    refused: bool
    error: FrappeError | None = None
    document_name: str | None = None


def probe_submit(config: SiteConfig, *, company: str) -> SubmitProbe:
    """Attempt a submit-on-insert with `config`'s credential; classify the result.

    Constructs its own `Client` with `may_submit=True`, deliberately lifting
    the local guard `stolon.frappe.client.Client` would otherwise apply: that
    guard would refuse this call before anything reached the network, which
    would prove nothing about the server's role permissions -- the only
    boundary this function exists to test. `config` itself is never mutated.

    The probe document is a `Journal Entry` with `docstatus: 1` and no
    `accounts` rows -- Frappe's own insert path submits a document created
    with `docstatus: 1` directly (`Document.check_docstatus_transition`, run
    from `check_if_latest()`, which both `insert()` and `_save()` call before
    any validation), so a permission check that passes still cannot post a
    real journal entry, for want of the mandatory child rows.

    Transport errors (a network failure, a non-Frappe HTTP error) propagate
    rather than being folded into a `SubmitProbe` -- they are not an answer
    about permissions, they are the probe failing to run at all.
    """
    probing_config = dataclasses.replace(config, may_submit=True)
    with Client(probing_config) as client:
        try:
            result = client.insert(
                _PROBE_DOCTYPE,
                {
                    "company": company,
                    "posting_date": "2000-01-01",
                    "voucher_type": "Journal Entry",
                    "docstatus": 1,
                },
            )
        except PermissionError_ as exc:
            return SubmitProbe(refused=True, error=exc)
        except FrappeError as exc:
            return SubmitProbe(refused=False, error=exc)
    return SubmitProbe(refused=False, document_name=result.get("name") if isinstance(result, dict) else None)
