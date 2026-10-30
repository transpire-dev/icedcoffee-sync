icedcoffee-sync is a typed client and sync engine for Frappe/ERPNext sites.

It provides a sync and async client for the Frappe REST API and a
diff/apply/prune engine that brings a site's documents in line with a declared
set.

## Development

Requires Python 3.14 and [uv](https://docs.astral.sh/uv/).

    just install     # create the virtualenv
    just check       # lock, lint, typecheck, deptry
    just test        # pytest, excluding tests that need a live site
    just lint-fix    # apply ruff fixes

Tests marked `live_frappe` hit a real Frappe site and need the
`ICEDCOFFEE_TEST_*` environment variables; `just test` skips them.
