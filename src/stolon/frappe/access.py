"""Provision Frappe roles, permissions, and users -- generically.

The client-side submit guard (`SiteConfig.may_submit`) is a courtesy any
caller can bypass; the boundary that holds is the API key's role permissions
on the server. This module builds that boundary -- a role whose flags never
include submit/cancel/amend, a user holding only that role -- and probes the
server to prove it. Callers supply doctypes, flags and names as policy.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Protocol

from stolon.frappe.client import Client, SiteConfig
from stolon.frappe.errors import DoesNotExistError, FrappeError, PermissionError_, ValidationError

# The Custom DocPerm flags a grant may request (Frappe version-15
# custom_docperm.json). `if_owner` and `select` are deliberately absent: see
# _ALWAYS_RESET_TO_ZERO.
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

# Flags `grant` always writes as "0": they change what a role's other flags
# mean (`if_owner=1` narrows every permission to documents the credential
# created), so a hand edit that set one must not survive a reconcile.
_ALWAYS_RESET_TO_ZERO: frozenset[str] = frozenset({"if_owner", "select"})

# Whitelisted server methods (Frappe version-15). The permission manager's
# methods and generate_keys require System Manager; generate_keys keeps an
# existing api_key and rotates api_secret on every call.
_PERMISSION_MANAGER = "frappe.core.page.permission_manager.permission_manager"
_GENERATE_KEYS = "frappe.core.doctype.user.user.generate_keys"
# frappe.client.has_permission(doctype, docname, perm_type) -> {"has_permission": bool}
_HAS_PERMISSION = "frappe.client.has_permission"

# The probe inserts this without its mandatory `accounts` rows, so even if
# permission passed, mandatory validation would stop anything posting.
PROBE_DOCTYPE = "Journal Entry"


@dataclasses.dataclass(frozen=True)
class PermissionGrant:
    """The flags to turn on for one doctype at one permission level; `grant` writes every other flag as "0"."""

    doctype: str
    ptypes: frozenset[str]
    permlevel: int = 0

    def __post_init__(self) -> None:
        unknown = sorted(self.ptypes - PTYPES)
        if unknown:
            raise ValueError(f"not a managed permission type: {', '.join(unknown)}; PTYPES is {sorted(PTYPES)}")
        if self.permlevel < 0:
            raise ValueError(f"permlevel must be 0 or more, not {self.permlevel}")


class _AccessClient(Protocol):
    """The slice of `Client` this module needs. Keeps fakes honest in tests."""

    def get(self, doctype: str, name: str) -> Any: ...
    def insert(self, doctype: str, doc: dict[str, Any]) -> Any: ...
    def update(self, doctype: str, name: str, doc: dict[str, Any]) -> Any: ...
    def call(self, dotted_path: str, params: dict[str, Any] | None = None) -> Any: ...


@dataclasses.dataclass(frozen=True)
class RoleChange:
    """What `ensure_role` did to reach the requested role, `desk_access`, and `disabled`."""

    created: bool
    desk_access_repaired: bool
    disabled_repaired: bool = False


def ensure_role(client: _AccessClient, role_name: str, *, desk_access: bool = False) -> RoleChange:
    """Create the role, or repair an existing one's `desk_access` and `disabled`.

    A hand-disabled or desk-enabled role is drift, not policy, so a re-run
    repairs it the way roles and grants are reconciled.
    """
    try:
        existing = client.get("Role", role_name)
    except DoesNotExistError:
        client.insert("Role", {"role_name": role_name, "desk_access": 1 if desk_access else 0})
        return RoleChange(created=True, desk_access_repaired=False)

    updates: dict[str, Any] = {}
    desk_access_repaired = bool(existing.get("desk_access")) != desk_access
    if desk_access_repaired:
        updates["desk_access"] = 1 if desk_access else 0
    disabled_repaired = bool(existing.get("disabled"))
    if disabled_repaired:
        updates["disabled"] = 0
    if updates:
        client.update("Role", role_name, updates)
    return RoleChange(created=False, desk_access_repaired=desk_access_repaired, disabled_repaired=disabled_repaired)


def grant(client: _AccessClient, role: str, permission_grant: PermissionGrant) -> list[tuple[str, str]]:
    """Write every managed flag for `role` on the doctype, "1" or "0", explicitly.

    Explicit zeros are the point: a flag left unmentioned would depend on a
    default this module does not control. Returns the (ptype, value) pairs sent.
    """
    # `add` is a no-op when the (doctype, role, permlevel) row already exists.
    client.call(
        f"{_PERMISSION_MANAGER}.add",
        {"parent": permission_grant.doctype, "role": role, "permlevel": permission_grant.permlevel},
    )
    sent: list[tuple[str, str]] = []
    for ptype in sorted(PTYPES | _ALWAYS_RESET_TO_ZERO):
        value = "1" if ptype in permission_grant.ptypes else "0"
        client.call(
            f"{_PERMISSION_MANAGER}.update",
            {
                "doctype": permission_grant.doctype,
                "role": role,
                "permlevel": permission_grant.permlevel,
                "ptype": ptype,
                "value": value,
            },
        )
        sent.append((ptype, value))
    return sent


def revoke_grants_except(client: _AccessClient, role: str, keep: frozenset[tuple[str, int]]) -> list[str]:
    """Revoke every permission row `role` holds except rows whose (doctype, permlevel) is in `keep`.

    Returns a label per revoked row.
    """
    rows = client.call(f"{_PERMISSION_MANAGER}.get_permissions", {"role": role})
    revoked: list[str] = []
    for row in rows:
        doctype = row["parent"]
        permlevel = row["permlevel"]
        if (doctype, permlevel) in keep:
            continue
        label = doctype if permlevel == 0 else f"{doctype} (permlevel {permlevel})"
        # Frappe refuses to remove a doctype's last permission rule; that
        # needs a person, so it is re-raised naming the row, not swallowed.
        try:
            client.call(
                f"{_PERMISSION_MANAGER}.remove",
                {"doctype": doctype, "role": role, "permlevel": permlevel},
            )
        except FrappeError as exc:
            raise type(exc)(
                f"could not revoke {label!r} from {role!r}: {exc.message}",
                error_type=exc.error_type,
                status=exc.status,
                messages=exc.messages,
                server_traceback=exc.server_traceback,
            ) from exc
        revoked.append(label)
    return revoked


@dataclasses.dataclass(frozen=True)
class UserChange:
    """What `ensure_user` did to reach the requested role set."""

    created: bool
    added_roles: frozenset[str]
    removed_roles: frozenset[str]
    enabled_repaired: bool = False


def ensure_user(client: _AccessClient, email: str, *, first_name: str, roles: frozenset[str]) -> UserChange:
    """Create the user, or reconcile an existing one's roles and `enabled` to policy.

    The role set is made exactly `roles`, not merged: a role added by hand is
    a privilege escalation riding on this key. A disabled user is drift for
    the same reason and is re-enabled.
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
    # An absent field means enabled (Frappe's default), not disabled.
    enabled_repaired = not existing.get("enabled", 1)
    updates: dict[str, Any] = {}
    if added or removed:
        updates["roles"] = [{"role": role} for role in sorted(roles)]
    if enabled_repaired:
        updates["enabled"] = 1
    if updates:
        client.update("User", email, updates)
    return UserChange(created=False, added_roles=added, removed_roles=removed, enabled_repaired=enabled_repaired)


@dataclasses.dataclass(frozen=True)
class ApiKeyPair:
    """A Frappe API key and secret. Kept out of reprs, like `SiteConfig`."""

    api_key: str = dataclasses.field(repr=False)
    api_secret: str = dataclasses.field(repr=False)


def generate_keys(client: _AccessClient, user: str) -> ApiKeyPair:
    """Mint a fresh API secret for `user` (the key is kept); a secret can never be read back."""
    result = client.call(_GENERATE_KEYS, {"user": user})
    return ApiKeyPair(api_key=result["api_key"], api_secret=result["api_secret"])


class OrphanProbeDocumentError(RuntimeError):
    """The probe's draft was created for real and could not be deleted afterwards.

    Distinct from a probe that merely failed: a real document now exists
    on the site and the probed credential (never granted delete) cannot
    remove it. Somebody has to.
    """

    def __init__(self, doctype: str, name: str, cleanup_error: FrappeError) -> None:
        super().__init__(
            f"probe created a real {doctype} document ({name!r}) that could not be deleted: {cleanup_error}"
        )
        self.doctype = doctype
        self.name = name
        self.cleanup_error = cleanup_error


@dataclasses.dataclass(frozen=True)
class SubmitProbe:
    """The outcome of attempting a submit-on-insert with a real credential.

    `refused` is True only when create access was proven by a real draft
    attempt (`can_create`), the server's per-flag answer says submit is
    denied (`submit_flag`), and the submit-flagged insert raised
    `PermissionError_`. Frappe's refusal text is the same whichever flag
    failed, so a 403 alone proves nothing about submit. `error` set to
    anything else means permission passed and validation stopped it;
    `document_name` set means something posted -- an incident, not a retry.
    """

    refused: bool
    can_create: bool | None = None
    submit_flag: bool | None = None
    error: FrappeError | None = None
    document_name: str | None = None


def _has_permission(client: _AccessClient, doctype: str, perm_type: str) -> bool:
    """The server's doctype-level answer to "does this credential hold `perm_type` on `doctype`"."""
    result = client.call(_HAS_PERMISSION, {"doctype": doctype, "docname": "", "perm_type": perm_type})
    return bool(result["has_permission"])


def prohibited_flags_allowed(config: SiteConfig, *, doctype: str, ptypes: frozenset[str]) -> dict[str, bool]:
    """{ptype: allowed} for each of `ptypes` on `doctype`, per the server; nothing is written.

    Frappe permissions are per doctype and per flag, so one call covers one
    doctype. A doctype-level answer is trustworthy for submit/cancel/amend
    (nothing document-scoped can widen a denied flag); it is not for
    create, which `probe_submit` proves with a real draft instead.
    """
    with Client(config) as client:
        return {ptype: _has_permission(client, doctype, ptype) for ptype in sorted(ptypes)}


def granted_flags_effective(config: SiteConfig, *, doctype: str, ptypes: frozenset[str]) -> dict[str, bool]:
    """{ptype: effective} for each granted flag on `doctype`, per the server; nothing is written.

    A Custom DocPerm row that exists and reads back is not proof it takes
    effect -- a `read` row on `DocType` does not, measured live -- so a
    positive grant is checked the same way a prohibited one is.

    **Blind to permlevel.** `frappe.client.has_permission` answers a
    doctype-level question and takes no permlevel of its own, so this cannot
    tell a permlevel-1 DocPerm row's presence from a permlevel-0 grant
    already on the same doctype -- a caller that asks this about a
    permlevel-1 grant gets back "effective" whether or not that row exists.
    Proving a grant above permlevel 0 needs a real round trip of a value
    through the credential instead; do not call this for one.
    """
    with Client(config) as client:
        return {ptype: _has_permission(client, doctype, ptype) for ptype in sorted(ptypes)}


def probe_submit(config: SiteConfig, *, company: str) -> SubmitProbe:
    """Attempt a submit-on-insert with `config`'s credential and classify the server's answer.

    The client-side guard is lifted on purpose (`may_submit=True`): only the
    server's refusal counts. Frappe checks the submit permission in
    `check_docstatus_transition` before any validation, and the probe
    document lacks its mandatory `accounts` rows, so nothing can post even
    if permission passes. Transport errors propagate: a probe that could
    not run is not an answer.
    """
    probing_config = dataclasses.replace(config, may_submit=True)
    with Client(probing_config) as client:
        submit_flag = _has_permission(client, PROBE_DOCTYPE, "submit")

        # Create access is proven by a real draft, not a doctype-level
        # has_permission: only the draft sees a Company-scoped restriction,
        # and a 403 from such a restriction would read as a submit refusal.
        create_error = _probe_create(client, company)
        if create_error is not None:
            return SubmitProbe(refused=False, can_create=False, submit_flag=submit_flag, error=create_error)

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
            refused = submit_flag is False
            return SubmitProbe(refused=refused, can_create=True, submit_flag=submit_flag, error=exc)
        except FrappeError as exc:
            return SubmitProbe(refused=False, can_create=True, submit_flag=submit_flag, error=exc)
    return SubmitProbe(
        refused=False,
        can_create=True,
        submit_flag=submit_flag,
        document_name=result.get("name") if isinstance(result, dict) else None,
    )


def _probe_create(client: Client, company: str) -> PermissionError_ | None:
    """Prove create access against `company` with a real draft; None means proven.

    Reaching validation (`ValidationError`) proves it -- for the missing
    `accounts` rows, or for whatever else the company's setup objects to
    first (on a perpetual-inventory company, ERPNext's
    `StockAccountInvalidTransaction`, read as a validation failure from its
    417); a `PermissionError_` is returned for the caller to report; anything
    else -- a rate limit, a 5xx -- propagates, since it answers nothing. A
    draft that unexpectedly lands is deleted at once.
    """
    try:
        draft = client.insert(
            PROBE_DOCTYPE,
            {"company": company, "posting_date": "2000-01-01", "voucher_type": "Journal Entry"},
        )
    except PermissionError_ as exc:
        return exc
    except ValidationError:
        return None
    name = draft.get("name") if isinstance(draft, dict) else None
    if name:
        try:
            client.delete(PROBE_DOCTYPE, name)
        except FrappeError as exc:
            raise OrphanProbeDocumentError(PROBE_DOCTYPE, name, exc) from exc
    return None
