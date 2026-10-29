"""Declarative apply: desired state, diff, ordered execution.

Generic by construction. Nothing here knows what a chart of accounts contains,
only how one is shaped. Dependency direction: `icedcoffee_sync.erpnext` may import from
this module, never the reverse.
"""
