# Fixture capture: Frappe v2 REST API on `transpire-test.frappe.cloud`

Captured 2026-08-24 by `icedcoffee-sync/tests/capture_fixtures.py` against a
disposable ERPNext v16 site ("Acmebike Cycling" company), key scoped for that
site only. Six scenario files, one per named fixture. Status codes actually
observed:

| scenario                  | intent                | status | what actually happened                                                         |
| ------------------------- | --------------------- | ------ | ------------------------------------------------------------------------------ |
| `list_ok`                 | list rows             | 200    | as expected                                                                    |
| `get_ok`                  | single-document GET   | 200    | as expected — fetches a real document fetched from a bootstrap list call       |
| `error_not_found`         | 404                   | 404    | as expected                                                                    |
| `permission_not_provoked` | 403/permission denied | 200    | **key is broadly privileged; no permission error provoked**                    |
| `error_validation`        | validation error      | 417    | validation error, but not the one the scenario name implies — see below        |
| `duplicate_not_provoked`  | duplicate-name error  | 417    | **hit the same validation error as `error_validation`, not a duplicate error** |

Two files are named for what they actually captured, not for the error class
they were originally intended to provoke — see "On the two `_not_provoked`
fixtures" below. Neither result was forced or invented; both are genuine
responses from this site with this API key, just not the response class their
first-guess name would suggest. **No fixture in this set demonstrates a real
permission-denied or duplicate-conflict error body.**

## `get_ok`: how it's captured

`get_ok` is a real `GET /api/v2/document/<Doctype>/<name>` call, not a list
call. The script bootstraps it: it first lists one `Account`
(`GET /api/v2/document/Account?limit=1`, not itself saved as a fixture), reads
`data[0]["name"]` from that response, URL-quotes it, and fetches
`GET /api/v2/document/Account/<quoted name>`. The quoting matters — account
names on this site contain spaces and parentheses (e.g.
`Application of Funds (Assets) - AB`), which are not valid unescaped path
characters.

## 1. Does a successful v2 list response wrap rows under `data`, or another key?

Both list and single-document responses use the key `data` — a list response
wraps rows in a JSON array under `data`; a single-document response puts the one
document directly under `data` as an object (not a one-element array).

`list_ok.json` body:

```json
{
  "has_next_page": true,
  "data": [
    { "name": "Application of Funds (Assets) - AB" },
    { "name": "Current Assets - AB" },
    { "name": "Accounts Receivable - AB" },
    { "name": "Debtors - AB" },
    { "name": "Bank Accounts - AB" }
  ]
}
```

`get_ok.json` body
(`GET /api/v2/document/Account/Application%20of%20Funds%20%28Assets%29%20-%20AB`):

```json
{
  "data": {
    "name": "Application of Funds (Assets) - AB",
    "owner": "Administrator",
    "creation": "2026-08-24 19:31:25.238170",
    "modified": "2026-08-24 19:31:25.238170",
    "modified_by": "Administrator",
    "docstatus": 0,
    "idx": 0,
    "disabled": 0,
    "account_name": "Application of Funds (Assets)",
    "account_number": "",
    "is_group": 1,
    "company": "Acmebike Cycling",
    "root_type": "Asset",
    "report_type": "Balance Sheet",
    "account_currency": "USD",
    "parent_account": null,
    "account_category": null,
    "account_type": "",
    "tax_rate": 0.0,
    "freeze_account": "No",
    "balance_must_be": "",
    "lft": 1,
    "rgt": 58,
    "old_parent": null,
    "include_in_gross": 0,
    "doctype": "Account"
  }
}
```

No `total`, `total_count`, or similar key is present anywhere in either envelope
— see question 2.

**This also answers the stated reason the plan targets v2 over v1: yes, v2
really does include empty and null fields on a single-document fetch.**
`account_number` and `account_type` are present as empty strings (`""`), and
`parent_account`, `account_category`, and `old_parent` are present as explicit
`null` — none of the five are omitted from the payload. A parser built against
this fixture can rely on every declared doctype field key being present in the
response, populated or not, rather than having to treat a missing key as
"unset."

## 2. What pagination parameters does v2 accept — `limit`/`offset`, or v1's `limit_start`/`limit_page_length`? Does the response carry a total?

Both list scenarios were sent with `limit` (`?limit=5` for `list_ok`, and the
`?limit=1` bootstrap call behind `get_ok`) and both succeeded with rows and a
`has_next_page` flag, so `limit` is accepted by v2. `offset` was not exercised
by any scenario in this capture, so its acceptance is unverified — worth a
targeted follow-up capture before Task 3/4 assume it works.

The response does **not** carry a total count of any kind. Pagination is
signalled purely via the boolean `has_next_page` — there is no `total`,
`total_count`, or page-count field anywhere in `list_ok.json`. A client has to
page forward until `has_next_page` is `false` to know when it's done; it can
never know the total row count up front. This matches the already-measured
baseline given in the task ("note the `has_next_page` flag"), so no surprise
here, but it directly answers "does it carry a total": **no**.

v1's `limit_start`/`limit_page_length` were not sent and so are not addressed by
this capture — the scenarios only exercise `limit`.

## 3. What exact keys does an error body have? Confirm whether `exc_type` and `_server_messages` are present, and capture one `_server_messages` value verbatim so its nesting is unambiguous.

**Neither `exc_type` nor `_server_messages` appears anywhere in any of the four
error-shaped fixtures.** Grepping every captured file for both strings returns
no matches. If Tasks 3–5 assumed either key exists in v2 error bodies (both are
v1-era Frappe conventions), that assumption is wrong for this endpoint family
and must be corrected against what's below — there is no `_server_messages`
value to quote verbatim because the key does not occur.

Two distinct error body shapes were captured, both under an `errors` array (not
a bare object):

**Shape A — no traceback** (`error_not_found.json`, status 404):

```json
{
  "errors": [
    {
      "type": "DoesNotExistError",
      "message": "Account NoSuchAccount-XYZ not found",
      "as_table": false,
      "title": "Message",
      "indicator": "red"
    }
  ]
}
```

Keys: `type`, `message`, `as_table`, `title`, `indicator`.

**Shape B — with traceback** (`error_validation.json` and
`duplicate_not_provoked.json`, both status 417):

```json
{
  "errors": [
    {
      "type": "ValidationError",
      "exception": "Traceback (most recent call last):\n  File \"apps/frappe/frappe/app.py\", line 121, in application\n ... frappe.exceptions.ValidationError: The root account <strong>- AB</strong> must be a group\n",
      "message": "The root account <strong>- AB</strong> must be a group",
      "as_table": false,
      "title": "Message",
      "indicator": "red"
    }
  ]
}
```

Keys: `type`, `exception` (full Python traceback as a single string, HTML
entities like `<strong>` left un-escaped inside `message`), `as_table`, `title`,
`indicator`. So the full key set observed across both shapes is `type`,
`message`, `as_table`, `title`, `indicator`, plus `exception` when the error
carries a traceback. `errors` is always a list (observed length 1 in every case,
never tested with multiple simultaneous errors).

## 4. What HTTP status accompanies each error class?

- `DoesNotExistError` (not-found): **404**
- `ValidationError` (both `error_validation` and `duplicate_not_provoked`):
  **417**
- Permission errors: **not captured.** `permission_not_provoked.json` returned
  **200** with real `User` data — this API key is broadly privileged on this
  disposable site, so `GET /api/v2/document/User?limit=1` never got denied.
  There is no fixture in this set demonstrating a 403/permission-denied body or
  its key shape. If Task 5 needs to handle a permission error class, it is
  building against zero real evidence from this capture — either re-run the
  capture with a deliberately restricted API key, or treat that error path as
  untested until then.

## On the two `_not_provoked` fixtures

Two scenarios were originally meant to provoke error classes distinct from
`error_not_found` and `error_validation` — a permission denial and a
duplicate-record conflict — but neither did, and the filenames now say so rather
than claiming otherwise:

**`permission_not_provoked.json`** — `GET /api/v2/document/User?limit=1`
returned 200 with a real row (`{"name": "analyst@example.com"}`). The API key
used for this capture is broadly privileged on the test site, so no request in
this scenario set was ever denied for permissions. This file is genuinely useful
as evidence that this key _cannot_ provoke a permission error on this site — it
is not evidence of what a permission-denied body looks like.

**`duplicate_not_provoked.json`** — intended to provoke a duplicate-name
conflict by posting an `Account` named `"Cash"`, which already exists on this
site. Instead it hit the **identical failure** as `error_validation`: posting
`{"account_name": ...}` to `/api/v2/document/Account` with no `parent_account`
creates a **root** account, and ERPNext's `Account.validate_root_details()`
rejects any root account that isn't a group, regardless of whether the name was
blank (`error_validation`) or a real existing name (`duplicate_not_provoked`,
"Cash"). Both come back as `type: "ValidationError"`, status 417, message "The
root account <strong>...</strong> must be a group". Posting `"Cash"` never
reached whatever check would actually detect a duplicate `name` — account
creation failed before that point.

**No fixture in this set demonstrates a real permission-denied response or a
real duplicate-name-conflict response.** If Task 5 needs to special-case either
error class, it needs a follow-up capture: a deliberately restricted API key for
permission denial, and — without inventing a scenario outside this site — a POST
that gets past root-account validation first (e.g. supply a `parent_account` and
`is_group: 0`) and only then collides on an existing `name`, for duplicate
detection.

## Summary of concerns for downstream tasks

- `_server_messages` and `exc_type` are Task 3–5 assumptions to drop — neither
  key exists in these v2 error bodies.
- Error bodies are shaped `{"errors": [...]}`, a list, not a single object.
- A single-document GET wraps the one document directly under `data` as an
  object (see `get_ok.json`), the same top-level key a list response uses for
  its array of rows — callers need to know which shape to expect from the
  request they made, `data` alone doesn't disambiguate.
- v2 includes every declared doctype field on a single-document GET, empty or
  null values included — nothing is omitted for being unset. See `get_ok.json`
  (`account_number: ""`, `parent_account: null`, etc.).
- `permission_not_provoked.json` and `duplicate_not_provoked.json` are real
  captures but do not demonstrate the error classes their original intent named
  — read their sections above before relying on either as evidence of an actual
  permission or duplicate error body.

## `purchase_order_submitted.json`

Captured 2026-09-03 by a one-off script against `example-demo-sites.invalid`
(Acmebike's demo ERPNext site): `GET /api/v2/document/Purchase Order/<name>` for
the Purchase Order carrying `transpire_order_no` `0001ABCD`, written raw and
unstripped, as `read_purchase_order` needs to parse the real shape.

Two fields were edited by hand after capture: **`docstatus`** (`0` → `1`) and
**`status`** (`"Draft"` → `"To Receive and Bill"`). The captured PO is still a
draft on the demo site — submitting it there needs a Supplier, an Item, and a
fiscal year set up beyond what Task 6 provisions — but
`test_erpnext_purchase.py` needs a fixture that reads as _submitted_ to exercise
`PurchaseOrder.is_submitted`/`.is_cancelled` and the
`order_confirmation_no`/`order_confirmation_date` fields the write-back plan
sets on a submitted order. Every other field, including every `items` row, is
exactly what the site returned.
