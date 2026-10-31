# icedcoffee-sync

icedcoffee-sync has three layers: a typed Frappe/ERPNext REST client,
idempotent document provisioning, and reconciliation of a declared set against
a site. This is version 0.1.0; today the diff/apply/prune engine handles charts
of accounts only. The intention is to extend that process to other document types.

[Source on GitHub](https://github.com/transpire-dev/icedcoffee-sync)

## Installation

Requires Python 3.14. To include pre-releases:

```sh
pip install --pre icedcoffee-sync
# or
uv add --prerelease allow icedcoffee-sync
```

## Client

`icedcoffee_sync.frappe.client` provides typed sync and async clients.
`SiteConfig.may_submit` defaults to false, guarding against accidental submits,
cancellations, known committing methods, and document writes that set `docstatus`.
The API key's server-side permissions enforce access.

Replace the site URL, API credentials, and document names in these examples.
Both clients return document dictionaries from reads and close connections on exit.

```python
from icedcoffee_sync.frappe.client import Client, SiteConfig

config = SiteConfig(
    url="https://erp.example.com",
    api_key="your-api-key",
    api_secret="your-api-secret",
)
with Client(config) as client:
    company = client.get("Company", "Example Company")
    print(company["name"])
```

The async equivalent:

```python
import asyncio
from icedcoffee_sync.frappe.client import AsyncClient, SiteConfig

config = SiteConfig(
    url="https://erp.example.com",
    api_key="your-api-key",
    api_secret="your-api-secret",
)

async def main():
    async with AsyncClient(config) as client:
        company = await client.get("Company", "Example Company")
        print(company["name"])

asyncio.run(main())
```

- `icedcoffee_sync.frappe.access`: provision roles, permission grants, users and API keys, and probe that a key cannot submit.
- `icedcoffee_sync.site.require_safe_site_url`: call before sending credentials. It refuses a URL that carries a credential, has no hostname, is not HTTPS on port 443, has a path, query or fragment past the site root, or names a host the caller refused. The clients do not call it for you.

## Document provisioning

`icedcoffee_sync.erpnext.ensure.ensure_by_name` gets a document by name and
inserts it only if missing, never rewriting one a person changed. It returns
true for an insert and false for an existing document. Supply a dict whose
inserted document will have the name you check. This get-or-insert pattern is
the provisioning layer for Supplier, Item, Purchase Order, Custom Field and
Webhook documents today.

```python
from icedcoffee_sync.erpnext.ensure import ensure_by_name
from icedcoffee_sync.frappe.client import Client, SiteConfig

config = SiteConfig(
    url="https://erp.example.com",
    api_key="your-api-key",
    api_secret="your-api-secret",
)
with Client(config) as client:
    created = ensure_by_name(client, "Item", "EXAMPLE-ITEM", {
        "item_code": "EXAMPLE-ITEM", "item_name": "Example Item",
        "item_group": "All Item Groups", "stock_uom": "Nos",
    })
    print(created)
```

- `icedcoffee_sync.erpnext.purchase`: typed Purchase Order reads and draft Purchase Receipt documents.
- `icedcoffee_sync.erpnext.company`: validate, set and clear Company default-account links.
- `icedcoffee_sync.erpnext.comment`: add and list comments on documents.

## Reconciliation

`icedcoffee_sync.apply` reconciles charts through the synchronous client.
Declare account numbers and parent numbers; roots must already exist and are
matched by root type. This example declares an expense root and one child.
Before pruning, declare **every account you want to keep** in the company.

**Warning:** `apply_chart` and `execute_prune` write to the site. Review each
printed plan before continuing; the example pauses before each write step.

```python
from icedcoffee_sync.apply.diff import diff
from icedcoffee_sync.apply.execute import apply_chart
from icedcoffee_sync.apply.model import DesiredAccount
from icedcoffee_sync.apply.prune import execute_prune, plan_prune
from icedcoffee_sync.erpnext.accounts import accounts_with_entries, company_abbr, read_chart
from icedcoffee_sync.frappe.client import Client, SiteConfig

config = SiteConfig(
    url="https://erp.example.com",
    api_key="your-api-key",
    api_secret="your-api-secret",
)
company = "Example Company"
desired = [
    DesiredAccount("5000", "Expenses", None, True, "Expense"),
    DesiredAccount("5202", "Bank Charges", "5000", False, "Expense"),
]
with Client(config) as client:
    live = read_chart(client, company)
    entries = accounts_with_entries(client, [a.document_name for a in live])
    plan = diff(desired, live, entries)
    print(plan)
    input("Review the diff; Enter to apply, Ctrl-C to stop: ")
    print(apply_chart(client, plan, live, {a.number: a for a in desired},
                      company_abbr(client, company), company))

    live = read_chart(client, company)
    entries = accounts_with_entries(client, [a.document_name for a in live])
    plan = diff(desired, live, entries)
    pruning = plan_prune([extra.live for extra in plan.extras], live, entries)
    print(pruning)
    input("Review the prune plan; Enter to execute, Ctrl-C to stop: ")
    print(execute_prune(client, pruning))
```

`diff` reports creates, renames, type changes, re-enables, extras and refusals
without writes. `apply_chart` runs changes in dependency order; any refusal
blocks the entire apply, and extras are left alone. `plan_prune` protects roots,
accounts with ledger entries and parents with surviving children.
`execute_prune` deletes selected extras, falling back to disabling them for
certain ERPNext business-rule errors.

## Development

Requires Python 3.14 and [uv](https://docs.astral.sh/uv/).

    just install     # create the virtualenv
    just check       # lock, lint, typecheck, deptry
    just test        # pytest, excluding tests that need a live site
    just lint-fix    # apply ruff fixes

Tests marked `live_frappe` hit a real Frappe site and need the
`ICEDCOFFEE_TEST_*` environment variables; `just test` skips them.
