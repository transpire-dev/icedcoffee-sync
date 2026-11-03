"""The names an integration writes onto a site under, supplied by the caller.

An integration that writes to an ERPNext site leaves its own name there: the
Custom Fields that mark the rows it wrote, the marker value in them, the names
of the Webhooks it provisions, and the header that tells its receiver which
site a delivery came from. None of those belong to this library, so every
function that writes or reads them takes a `Namespace`.

Changing a namespace for a site that already has documents written under it
orphans them: the old Custom Fields and Webhooks stay on the site under the old
names, and nothing here recognises them any more.
"""

from __future__ import annotations

import dataclasses
import re

# A Frappe fieldname: lowercase, starting with a letter. The namespace name
# prefixes every Custom Field, so it has to be one too.
_FIELDNAME = re.compile(r"[a-z][a-z0-9_]*")
# An HTTP header name (RFC 9110 token), narrowed to what a person would type.
_HEADER = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*")


@dataclasses.dataclass(frozen=True)
class Namespace:
    """Who is writing to the site.

    `name` prefixes every Custom Field (`<name>_source`, `<name>_ref`,
    `<name>_order_no`) and every Webhook name, and is the marker value stamped
    into `<name>_source`. `label` prefixes the Custom Fields' labels as a
    person sees them on the form. `site_header` is the request header each
    Webhook carries the caller's site id in.
    """

    name: str
    label: str
    site_header: str

    def __post_init__(self) -> None:
        if not _FIELDNAME.fullmatch(self.name):
            raise ValueError(f"namespace name {self.name!r} is not a Frappe fieldname (lowercase, letters first)")
        if not self.label.strip():
            raise ValueError("namespace label is empty")
        if not _HEADER.fullmatch(self.site_header):
            raise ValueError(f"site header {self.site_header!r} is not an HTTP header name")

    @property
    def marker(self) -> str:
        """The value stamped into `source_field` on every document written."""
        return self.name

    @property
    def source_field(self) -> str:
        return f"{self.name}_source"

    @property
    def ref_field(self) -> str:
        return f"{self.name}_ref"

    @property
    def order_no_field(self) -> str:
        return f"{self.name}_order_no"
