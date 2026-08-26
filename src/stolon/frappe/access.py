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
PROBE_DOCTYPE = "Journal Entry"

# Frappe's whitelisted per-flag permission check (`frappe/client.py`,
# `version-15`): `def has_permission(doctype: str, docname: str | int,
# perm_type: str = "read")`, returning `{"has_permission": bool}` from
# `frappe.has_permission(doctype, perm_type.lower(), docname)`. Read-only --
# not a member of `Client._COMMITTING_METHODS`, so it runs regardless of
# `may_submit`.
_HAS_PERMISSION = "frappe.client.has_permission"


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


def revoke_grants_except(client: _AccessClient, role: str, keep: frozenset[str]) -> list[str]:
    """Remove every permission row `role` holds on a doctype outside `keep`.

    Reconciles grants the way `ensure_user` reconciles roles: a policy that
    only ever adds permissions and never retracts ones dropped from a
    caller's grant list would let stale access accumulate silently across
    runs. Lists the role's current rows via the permission manager's
    `get_permissions` (each row names its doctype as `parent`, plus its
    `permlevel`), then calls `remove` for every row whose `parent` is not in
    `keep`.

    Frappe refuses outright to remove a doctype's last permission rule
    ("There must be atleast one permission rule." -- `remove`'s own guard
    against leaving a doctype with zero rows) -- if `role` is the only role
    holding any rule on that doctype, the row cannot be revoked this way.
    That is re-raised naming the doctype, not swallowed: a stale grant
    Frappe will not let go of on its own needs a human to look at it, not a
    silent partial revoke.

    Returns:
        The doctypes actually revoked, in the order `get_permissions`
        listed them.
    """
    rows = client.call(f"{_PERMISSION_MANAGER}.get_permissions", {"role": role})
    revoked: list[str] = []
    for row in rows:
        doctype = row["parent"]
        if doctype in keep:
            continue
        try:
            client.call(
                f"{_PERMISSION_MANAGER}.remove",
                {"doctype": doctype, "role": role, "permlevel": row["permlevel"]},
            )
        except FrappeError as exc:
            raise type(exc)(
                f"could not revoke {doctype!r} from {role!r}: {exc.message}",
                error_type=exc.error_type,
                status=exc.status,
                messages=exc.messages,
                server_traceback=exc.server_traceback,
            ) from exc
        revoked.append(doctype)
    return revoked


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

    `can_create` and `submit_flag` are the server's own answers -- via the
    whitelisted `frappe.client.has_permission`, asked before the insert
    attempt -- to "can this credential create `PROBE_DOCTYPE` at all" and
    "can it submit `PROBE_DOCTYPE`". They exist because a `PermissionError`
    from the insert cannot be trusted alone: measured on a live site
    (2026-08-25), Frappe's refusal text is the same generic
    `push_perm_check_log` message -- "User X does not have doctype access via
    role permission for document Journal Entry" -- whether the missing flag
    was `create` or `submit`. Without the two-flag check, a bot locked out of
    drafting entirely looks identical to one correctly scoped to
    draft-but-not-submit: both raise `PermissionError_` on the same insert,
    and the first live run of this probe stored a credential on exactly that
    false-positive evidence.

    `refused=True` requires all three: `can_create is True`,
    `submit_flag is False`, and the insert actually raising
    `PermissionError_`. That is the only combination that proves the
    refusal specifically targets submit rather than being collateral from a
    wider lockout.

    Every other combination is `refused=False`:

    - `can_create is False`: the credential cannot even create a draft, so
      this probe proves nothing about submit scoping -- the insert's
      `PermissionError_`, if any, may just be the create lockout showing up
      first. This is the ambiguity the live run above could not resolve.
    - `error` set to something other than `PermissionError_` (typically
      `ValidationError`): permission passed and Frappe's own mandatory-field
      validation stopped it instead -- proof of nothing about submit
      permission, since validation never got the chance to matter.
    - `document_name` set is the catastrophic case: permission passed, and
      despite the probe document deliberately omitting the mandatory
      `accounts` rows, something actually posted. Treat this as an incident,
      not a retry.

    `can_create`/`submit_flag` are `None` only if a `SubmitProbe` is
    constructed without them; `probe_submit` itself always sets both, since a
    `has_permission` call that fails propagates rather than producing a
    probe -- see `probe_submit`.
    """

    refused: bool
    can_create: bool | None = None
    submit_flag: bool | None = None
    error: FrappeError | None = None
    document_name: str | None = None


def _has_permission(client: _AccessClient, doctype: str, perm_type: str) -> bool:
    """Ask the server whether this credential holds `perm_type` on `doctype`.

    `docname=""`: this is a doctype-level question -- no document exists yet
    for this probe to name. Errors here are not caught: a `has_permission`
    call that itself fails means the probe could not run at all, the same as
    a transport error would, and `probe_submit` lets it propagate.
    """
    result = client.call(_HAS_PERMISSION, {"doctype": doctype, "docname": "", "perm_type": perm_type})
    return bool(result["has_permission"])


def probe_submit(config: SiteConfig, *, company: str) -> SubmitProbe:
    """Attempt a submit-on-insert with `config`'s credential; classify the result.

    Constructs its own `Client` with `may_submit=True`, deliberately lifting
    the local guard `stolon.frappe.client.Client` would otherwise apply: that
    guard would refuse this call before anything reached the network, which
    would prove nothing about the server's role permissions -- the only
    boundary this function exists to test. `config` itself is never mutated.

    Before attempting the insert, asks the server directly (via
    `frappe.client.has_permission`, which writes nothing) whether this
    credential can `create` and whether it can `submit` `PROBE_DOCTYPE` --
    see `SubmitProbe` for why a bare `PermissionError` from the insert cannot
    be trusted alone to mean "submit is the thing that's missing".

    The probe document is a `Journal Entry` with `docstatus: 1` and no
    `accounts` rows -- Frappe's own insert path submits a document created
    with `docstatus: 1` directly (`Document.check_docstatus_transition`, run
    from `check_if_latest()`, which both `insert()` and `_save()` call before
    any validation), so a permission check that passes still cannot post a
    real journal entry, for want of the mandatory child rows.

    Transport errors, and a `has_permission` call itself failing, propagate
    rather than being folded into a `SubmitProbe` -- neither is an answer
    about permissions, both are the probe failing to run at all.
    """
    probing_config = dataclasses.replace(config, may_submit=True)
    with Client(probing_config) as client:
        can_create = _has_permission(client, PROBE_DOCTYPE, "create")
        submit_flag = _has_permission(client, PROBE_DOCTYPE, "submit")
        try:
            result = client.insert(
                PROBE_DOCTYPE,
                {
                    "company": company,
                    "posting_date": "2000-01-01",
                    "voucher_type": "Journal Entry",
                    "docstatus": 1,
                },
            )
        except PermissionError_ as exc:
            refused = can_create is True and submit_flag is False
            return SubmitProbe(refused=refused, can_create=can_create, submit_flag=submit_flag, error=exc)
        except FrappeError as exc:
            return SubmitProbe(refused=False, can_create=can_create, submit_flag=submit_flag, error=exc)
    return SubmitProbe(
        refused=False,
        can_create=can_create,
        submit_flag=submit_flag,
        document_name=result.get("name") if isinstance(result, dict) else None,
    )
