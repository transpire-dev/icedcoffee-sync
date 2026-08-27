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
from stolon.frappe.errors import DoesNotExistError, FrappeError, PermissionError_, ValidationError

# The Custom DocPerm flags a caller may request in a `PermissionGrant`. No
# grant this module writes ever needs `if_owner` or `select` (see
# `_ALWAYS_RESET_TO_ZERO` below for why they still can't be left alone) --
# if a caller ever needs to actually grant one, it belongs as a deliberate
# addition here, not a silent gap that lets a caller ask for it unchecked.
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

# Custom DocPerm flags `grant` resets to "0" unconditionally, on top of
# `PTYPES` -- never requestable, because they change what a role's *other*
# flags mean rather than adding a capability. `if_owner=1` on an otherwise
# fully-granted role silently narrows every one of those permissions to
# documents the credential itself created, which is invisible to anyone
# reading `PermissionGrant.ptypes` and not something the explicit-zeros
# reasoning above ever covered, since PTYPES never mentions it at all. A
# role reconciled by `grant`/`revoke_grants_except` must not depend on
# whichever value some other process (a hand edit through Frappe's UI, most
# plausibly) left on a row this module didn't create.
_ALWAYS_RESET_TO_ZERO: frozenset[str] = frozenset({"if_owner", "select"})

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


@dataclasses.dataclass(frozen=True)
class RoleChange:
    """What `ensure_role` did to reach the requested role, `desk_access`, and `disabled`."""

    created: bool
    desk_access_repaired: bool
    disabled_repaired: bool = False


def ensure_role(client: _AccessClient, role_name: str, *, desk_access: bool = False) -> RoleChange:
    """Create `role_name` if it does not already exist; repair `desk_access` and `disabled` if it does and differ.

    An existing role's `desk_access` used to be left alone entirely --
    reconciled on create only, never on a re-run. A role hand-flipped to
    `desk_access=1` through Frappe's UI since the last provision would
    silently keep desk login enabled forever, the same class of drift
    `revoke_grants_except` and `ensure_user`'s role reconciliation both
    already close for permissions and roles respectively.

    An existing role found disabled is re-enabled the same way, in the same
    update: a role hand-disabled through Frappe's UI (or by an incident
    response) would otherwise silently strip the bot of every permission it
    holds, forever -- this bot has no legitimate reason to have its role
    disabled by policy, so that state can only be drift, the same as
    `ensure_user`'s own `enabled` reconciliation already treats a disabled
    user.

    Args:
        client: A Frappe client with System Manager permissions.
        role_name: The role's name, which is also its `role_name` field --
            Role auto-names from that field.
        desk_access: Whether the role can log into the desk UI at all. False
            for a pure API-key bot: it never opens a browser session.

    Returns:
        A `RoleChange` recording whether the role was created, whether an
        existing role's `desk_access` had to be repaired to match, and
        whether an existing role found disabled had to be re-enabled.
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

    `if_owner` and `select` are reset to "0" the same explicit way, on top
    of `PTYPES` -- see `_ALWAYS_RESET_TO_ZERO` for why leaving either one
    unmentioned is not safe to do, unlike an ordinary ptype nobody asked for.

    Returns:
        The (ptype, value) pairs sent, one per member of `PTYPES` plus
        `_ALWAYS_RESET_TO_ZERO`.
    """
    client.call(
        f"{_PERMISSION_MANAGER}.add",
        {"parent": permission_grant.doctype, "role": role, "permlevel": 0},
    )
    sent: list[tuple[str, str]] = []
    for ptype in sorted(PTYPES | _ALWAYS_RESET_TO_ZERO):
        value = "1" if ptype in permission_grant.ptypes else "0"
        client.call(
            f"{_PERMISSION_MANAGER}.update",
            {"doctype": permission_grant.doctype, "role": role, "permlevel": 0, "ptype": ptype, "value": value},
        )
        sent.append((ptype, value))
    return sent


def revoke_grants_except(client: _AccessClient, role: str, keep: frozenset[str]) -> list[str]:
    """Remove every permission row `role` holds outside the `(doctype, 0)` pairs in `keep`.

    Reconciles grants the way `ensure_user` reconciles roles: a policy that
    only ever adds permissions and never retracts ones dropped from a
    caller's grant list would let stale access accumulate silently across
    runs. Lists the role's current rows via the permission manager's
    `get_permissions` (each row names its doctype as `parent`, plus its
    `permlevel`), then calls `remove` for every row that is not a permlevel-0
    row on a doctype in `keep`.

    permlevel matters here because `grant` only ever manages permlevel 0:
    every `update` call it makes hardcodes `permlevel: 0`. A row at any
    other permlevel on an otherwise-allowed doctype is therefore never
    reset by `grant` and would survive here forever if this function kept
    it just because its doctype is allowed -- silently carrying whatever
    flags (potentially submit, cancel, or amend among them) some other
    channel set on it, in direct tension with this module's whole point.
    Nothing this policy declares ever needs a nonzero permlevel, so every
    such row is revoked unconditionally, regardless of `keep`.

    Frappe refuses outright to remove a doctype's last permission rule
    ("There must be atleast one permission rule." -- `remove`'s own guard
    against leaving a doctype with zero rows) -- if `role` is the only role
    holding any rule on that doctype, the row cannot be revoked this way.
    That is re-raised naming the doctype, not swallowed: a stale grant
    Frappe will not let go of on its own needs a human to look at it, not a
    silent partial revoke.

    Returns:
        A label per row actually revoked, in the order `get_permissions`
        listed them -- the bare doctype name for a permlevel-0 revoke, or
        `"{doctype} (permlevel {permlevel})"` for a nonzero one, so the
        two cases read distinctly in `books`' printed report.
    """
    rows = client.call(f"{_PERMISSION_MANAGER}.get_permissions", {"role": role})
    revoked: list[str] = []
    for row in rows:
        doctype = row["parent"]
        permlevel = row["permlevel"]
        if doctype in keep and permlevel == 0:
            continue
        label = doctype if permlevel == 0 else f"{doctype} (permlevel {permlevel})"
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
    """Create the user if missing, or reconcile its roles (and `enabled`) to policy.

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

    An existing user found disabled (`enabled: 0`) is re-enabled the same
    way: this bot has no legitimate reason to be disabled by policy, so a
    disabled state can only be drift (an incident response, a hand edit)
    that a re-run should repair like any other. Left unrepaired, `provision`
    would still mint a fresh secret for a user that cannot authenticate
    with it at all, fail verification, and repeat that on every subsequent
    run with no way to recover short of a human re-enabling the user
    directly -- exactly the kind of drift this function's role reconciliation
    already exists to fix, just missed for this one field.

    Returns:
        A `UserChange` recording whether the user was created, which roles
        were added or removed to reach `roles`, and whether a disabled user
        had to be re-enabled.
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
    # Defaults the assumed value to enabled when the field is absent from
    # `existing` -- matching Frappe's own default for a User -- rather than
    # treating absence itself as disabled. A real `client.get("User", ...)`
    # always includes `enabled` either way; this only matters for a caller
    # (or a fake in tests) that reads back a partial document.
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
    """Issue a fresh API secret for `user`, keeping any existing api_key.

    Calls Frappe's whitelisted `generate_keys` method. Caller needs System
    Manager. Rotates `api_secret` on every call -- there is no way to read
    back a previously issued secret, only to mint a new one.
    """
    result = client.call(_GENERATE_KEYS, {"user": user})
    return ApiKeyPair(api_key=result["api_key"], api_secret=result["api_secret"])


class OrphanProbeDocumentError(RuntimeError):
    """A create-access probe's draft was created for real, and cleanup could not remove it.

    `_probe_create` expects its draft to stop at `ValidationError` (missing
    mandatory rows) before ever needing to write anything back out. The
    rarer path -- the insert actually succeeds -- is already documented as
    an incident, not a retry; this is what happens when that incident's
    own cleanup then also fails, most plausibly because the credential
    being probed is deliberately never granted delete on the probe
    doctype (a bot minted by `books.access.provision`, say). Raised
    instead of letting the delete's own `FrappeError` propagate
    unlabeled: that error alone reads as "the probe failed", which hides
    the actually urgent fact -- a real document now exists on the live
    site and nothing removed it.
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

    `submit_flag` is the server's own answer -- via the whitelisted
    `frappe.client.has_permission`, asked before either insert attempt --
    to "can this credential submit `PROBE_DOCTYPE`". It exists because a
    `PermissionError` from the insert cannot be trusted alone: measured
    on a live site (2026-08-25), Frappe's refusal text is the same
    generic `push_perm_check_log` message -- "User X does not have
    doctype access via role permission for document Journal Entry" --
    whether the missing flag was `create` or `submit`.

    `can_create` is never asked of `has_permission` at all: that would be
    a doctype-level question (`docname=""`) and cannot see a document- or
    Company-scoped restriction that a real insert would hit. Instead
    `_probe_create` proves create access directly by actually attempting
    the document as a draft, and `can_create` is exactly its result.
    Without that check, a bot narrowed to a different Company, or locked
    out of drafting entirely, would look identical from a doctype-level
    answer alone to one correctly scoped to draft-but-not-submit: both
    can raise `PermissionError_` on the same submit-flagged insert, and
    the first live run of this probe stored a credential on exactly that
    class of false-positive evidence (a total lockout, in that case, not
    yet a Company restriction, but the same doctype-level blind spot).

    `refused=True` requires all three: `can_create is True`,
    `submit_flag is False`, and the submit-flagged insert actually
    raising `PermissionError_`. That is the only combination that proves
    the refusal specifically targets submit rather than being collateral
    from a wider or narrower lockout.

    Every other combination is `refused=False`:

    - `can_create is False`: the credential cannot create a real draft
      against this company, so this probe proves nothing about submit
      scoping -- a `PermissionError_` from the submit-flagged insert, had
      it been attempted, might just be the same create-side restriction
      showing up again.
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


def prohibited_flags_allowed(config: SiteConfig, *, doctype: str, ptypes: frozenset[str]) -> dict[str, bool]:
    """Ask the server, doctype-level, whether this credential holds any of `ptypes` on `doctype`.

    Built on the same whitelisted `frappe.client.has_permission` that
    `probe_submit`'s own `submit_flag` uses -- no probe document, no insert,
    nothing written. That doctype-level answer is not trustworthy for
    `create` (see `probe_submit` and `SubmitProbe`'s docstrings: a document-
    or Company-scoped restriction can make a doctype-level "yes" wrong), but
    `submit`/`cancel`/`amend` are exactly the Custom DocPerm flags `grant`
    sets explicitly and unconditionally to "0" for a role this module
    manages, with no `if_owner` scoping surviving to narrow them --
    `_ALWAYS_RESET_TO_ZERO` resets that to "0" every run too. A doctype-level
    answer is therefore trustworthy for these three flags in a way it is not
    for create, and needs no probe document at all -- useful for a doctype
    this module has no measured, always-invalid document shape for, unlike
    `PROBE_DOCTYPE`'s missing-`accounts` gap.

    Frappe permissions are per doctype and per flag: a credential proven
    refused on one doctype's submit is not evidence about another doctype's
    cancel, or about amend on either. A caller granting more than one
    submittable doctype needs one call per doctype to cover all of them.

    Returns:
        {ptype: has_permission} for every member of `ptypes`.
    """
    with Client(config) as client:
        return {ptype: _has_permission(client, doctype, ptype) for ptype in sorted(ptypes)}


def granted_flags_effective(config: SiteConfig, *, doctype: str, ptypes: frozenset[str]) -> dict[str, bool]:
    """Ask the server, doctype-level, whether this credential actually holds each of `ptypes` on `doctype`.

    `prohibited_flags_allowed` turned around: same `has_permission` call,
    same doctype-level scope, but built for the opposite question -- not
    "is a flag that should be denied still allowed", but "is a flag
    `grant` set to '1' actually in effect". Exists because a Custom DocPerm
    row that exists, and reads back correctly from
    `permission_manager.get_permissions`, is not proof it takes effect for
    this user's own `has_permission` answer: measured live (2026-08-25),
    the `DocType` doctype did exactly that -- a granted `read` row present
    and correctly reported, refused anyway (see `books/access.py`'s note on
    why `DocType: read` was dropped from its `GRANTS` entirely). A caller
    storing a credential on an unverified positive grant risks the same
    silent gap on any other doctype.

    Shares `prohibited_flags_allowed`'s doctype-level blind spot for
    `create`: a document- or Company-scoped restriction can make a
    doctype-level "yes" wrong the same way `probe_submit`'s own docstring
    warns about. Accepted here rather than worked around with a real
    create attempt -- a mutating probe against the ledger is exactly what
    a positive-grant check must never become.

    Returns:
        {ptype: has_permission} for every member of `ptypes`.
    """
    with Client(config) as client:
        return {ptype: _has_permission(client, doctype, ptype) for ptype in sorted(ptypes)}


def probe_submit(config: SiteConfig, *, company: str) -> SubmitProbe:
    """Attempt a submit-on-insert with `config`'s credential; classify the result.

    Constructs its own `Client` with `may_submit=True`, deliberately lifting
    the local guard `stolon.frappe.client.Client` would otherwise apply: that
    guard would refuse this call before anything reached the network, which
    would prove nothing about the server's role permissions -- the only
    boundary this function exists to test. `config` itself is never mutated.

    Before attempting the insert, asks the server directly (via
    `frappe.client.has_permission`, which writes nothing) whether this
    credential can `submit` `PROBE_DOCTYPE` -- see `SubmitProbe` for why a
    bare `PermissionError` from the insert cannot be trusted alone to mean
    "submit is the thing that's missing".

    There is no equivalent `has_permission(..., "create")` ask: that
    question is doctype-level (`docname=""`) and cannot see a document- or
    Company-scoped restriction -- a User Permission narrowing which
    Company values this credential may create against, for instance.
    Trusting a doctype-level "yes" would let exactly that kind of
    restriction surface as a `PermissionError_` on the submit attempt
    below and get misread as a submit-scoped refusal, storing a
    credential that cannot actually draft anything for this company. So
    `_probe_create` attempts the identical document as a real draft (no
    `docstatus`) first, and only a `PermissionError_` from the submit
    attempt after that draft demonstrably succeeded -- reached
    validation, or actually inserted, in which case the draft is deleted
    immediately -- is trusted to be about submit specifically.

    The probe document is a `Journal Entry` with no `accounts` rows --
    Frappe's own insert path submits a document created with `docstatus:
    1` directly (`Document.check_docstatus_transition`, run from
    `check_if_latest()`, which both `insert()` and `_save()` call before
    any validation), so a permission check that passes still cannot post
    a real journal entry, for want of the mandatory child rows; the same
    missing rows are expected to stop the draft attempt at validation too,
    for the same reason.

    Transport errors, and a `has_permission` call itself failing, propagate
    rather than being folded into a `SubmitProbe` -- neither is an answer
    about permissions, both are the probe failing to run at all.
    """
    probing_config = dataclasses.replace(config, may_submit=True)
    with Client(probing_config) as client:
        submit_flag = _has_permission(client, PROBE_DOCTYPE, "submit")

        # No doctype-level `has_permission(..., "create")` ask here: it
        # cannot see a document- or Company-scoped restriction, so its
        # answer would be no better than a guess about `can_create` --
        # `_probe_create`'s real draft attempt is strictly more trustworthy
        # and this probe writes nothing extra to get it.
        create_error = _probe_create(client, company)
        if create_error is not None:
            # can_create=False is proven directly by the draft's own
            # refusal, so nothing below -- including a PermissionError
            # from the submit attempt -- can be trusted to be about
            # submit rather than this same restriction.
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
    """Prove create access for real, against `company` specifically.

    Attempts the same probe document as `probe_submit`, minus `docstatus`
    -- a genuine draft. Returns `None` once document-level create access
    is confirmed: the insert reached validation (a `ValidationError`,
    since the document is missing its mandatory `accounts` rows -- the
    same gap `probe_submit` relies on to stop the submit-flagged attempt)
    or, less expectedly, succeeded outright, in which case the draft it
    created is deleted immediately so this probe leaves nothing behind.
    Returns the `PermissionError_` itself, for the caller to report, only
    when the attempt raises one: the doctype-level `has_permission` check
    said create was allowed, but this document-level attempt disagrees,
    so the restriction is real and nothing downstream can be attributed
    to submit instead.

    Only `ValidationError` counts as reaching validation -- not
    `except FrappeError` generally. A `RateLimitError` or an unclassified
    5xx on this insert answers nothing about permissions; treating it the
    same as a `ValidationError` would let the submit-flagged attempt run
    and have its own result trusted even though create access was never
    actually proven. Those propagate uncaught, the same as `probe_submit`
    already lets a transport error or a failed `has_permission` call
    propagate: a probe that could not run is not an answer, in either
    direction.

    The cleanup delete uses `client` itself -- the very credential being
    probed, not an administrator's. That is deliberate everywhere the
    draft attempt stops at `ValidationError`, since the delete never
    runs; it becomes a real gap only on the rare path where the insert
    unexpectedly succeeds, and the credential being probed (a bot minted
    by `books.access.provision`, most plausibly) is never granted delete
    on the probe doctype by design. Raises `OrphanProbeDocumentError`
    rather than letting that delete's own `FrappeError` propagate
    unlabeled: an unlabeled permission error there reads as "the probe
    failed", burying the fact that a real document now exists on the
    live site and nothing removed it.
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
