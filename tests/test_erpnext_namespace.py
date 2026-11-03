"""The caller's namespace: every name an integration leaves on a site."""

import pytest

from icedcoffee_sync.erpnext.namespace import Namespace


def test_the_field_names_and_marker_come_from_the_name() -> None:
    ns = Namespace(name="acme", label="Acme", site_header="X-Acme-Site")
    assert ns.marker == "acme"
    assert (ns.source_field, ns.ref_field, ns.order_no_field) == ("acme_source", "acme_ref", "acme_order_no")


@pytest.mark.parametrize("name", ["", "Acme", "1acme", "acme-sync", "acme sync", "_acme"])
def test_a_name_that_is_not_a_frappe_fieldname_is_refused(name: str) -> None:
    with pytest.raises(ValueError, match="not a Frappe fieldname"):
        Namespace(name=name, label="Acme", site_header="X-Acme-Site")


def test_an_empty_label_is_refused() -> None:
    with pytest.raises(ValueError, match="label is empty"):
        Namespace(name="acme", label="  ", site_header="X-Acme-Site")


@pytest.mark.parametrize("header", ["", "X Acme Site", "X-Acme-Site:", "-X-Acme", "X_Acme"])
def test_a_site_header_that_is_not_a_header_name_is_refused(header: str) -> None:
    with pytest.raises(ValueError, match="not an HTTP header name"):
        Namespace(name="acme", label="Acme", site_header=header)
