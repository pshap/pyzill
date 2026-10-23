"""The compatibility shims that keep historical import paths working."""

import importlib
import warnings

import pytest

from pyzill import api, config
from pyzill import http as pyzill_http


def _import_with_warning(name: str) -> object:
    """Import (or reload) a module and return it, asserting the deprecation."""
    with pytest.warns(DeprecationWarning, match=name):
        return importlib.reload(importlib.import_module(name))


def test_search_module_is_a_deprecated_shim():
    """`pyzill.search` still works, but says where the entry points live now."""
    module = _import_with_warning("pyzill.search")

    assert vars(module)["for_sale"] is api.for_sale
    assert vars(module)["for_rent"] is api.for_rent
    assert vars(module)["sold"] is api.sold


def test_details_module_is_a_deprecated_shim():
    """`pyzill.details` still works, but says where the fetchers live now."""
    module = _import_with_warning("pyzill.details")

    assert vars(module)["get_from_home_id"] is api.get_from_home_id
    assert vars(module)["get_from_apartment_url"] is api.get_from_apartment_url


def test_http_zillow_home_reexport_is_deprecated():
    """`pyzill.http.ZILLOW_HOME` warns and resolves the config value."""
    with pytest.warns(DeprecationWarning, match="pyzill.config"):
        assert pyzill_http.ZILLOW_HOME == config.ZILLOW_HOME


def test_http_still_rejects_unknown_attributes():
    """The module-level `__getattr__` only serves the deprecated name."""
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with pytest.raises(AttributeError, match="ZILLOW_ORIGIN"):
            _ = pyzill_http.ZILLOW_ORIGIN
