import pytest

from pyzill import utils


def test_remove_space():
    assert utils.remove_space("  hello   world  ") == "hello world"
    assert utils.collapse_whitespace("  hello   world  ") == "hello world"


def test_get_nested_value():
    data = {"a": {"b": {"c": 42}}}
    assert utils.get_nested_value(data, "a.b.c") == 42
    assert utils.get_nested_value(data, "a.b.missing", default="x") == "x"
    assert utils.get_nested_value(data, "z.y") is None


def test_parse_proxy_encodes_credentials():
    url = utils.parse_proxy("host.com", "8080", "user@name", "p@ss/word")
    assert url.startswith("http://")
    assert "user%40name" in url
    assert "p%40ss%2Fword" in url
    assert url.endswith("@host.com:8080")


def test_parse_proxy_supports_scheme_and_ipv6():
    url = utils.parse_proxy("2001:db8::1", "8080", "user", "pass", scheme="socks5h")

    assert url == "socks5h://user:pass@[2001:db8::1]:8080"


@pytest.mark.parametrize("port", ["0", "65536", "abc"])
def test_parse_proxy_rejects_invalid_ports(port):
    with pytest.raises(ValueError, match="port"):
        utils.parse_proxy("host.example", port, "user", "pass")


def test_parse_proxy_rejects_invalid_scheme():
    with pytest.raises(ValueError, match="scheme"):
        utils.parse_proxy("host.example", "8080", "user", "pass", scheme="ftp")


def test_resolve_proxy_explicit_wins(monkeypatch):
    monkeypatch.setenv("ZILLOW_PROXY", "http://env:1@envhost:1")
    assert utils.resolve_proxy("http://x:y@host:1") == "http://x:y@host:1"


def test_pyzill_env_takes_precedence_over_legacy(monkeypatch):
    monkeypatch.setenv("PYZILL_PROXY", "https://canonical.example:443")
    monkeypatch.setenv("ZILLOW_PROXY", "http://legacy.example:80")

    assert utils.resolve_proxy() == "https://canonical.example:443"


def test_resolve_proxy_env_fallback(monkeypatch):
    monkeypatch.setenv("ZILLOW_PROXY", "http://env:1@envhost:1")
    assert utils.resolve_proxy() == "http://env:1@envhost:1"


def test_resolve_proxy_none(monkeypatch):
    monkeypatch.delenv("ZILLOW_PROXY", raising=False)
    assert utils.resolve_proxy() is None


def test_resolve_proxy_invalid_scheme():
    with pytest.raises(ValueError, match="scheme"):
        utils.resolve_proxy("ftp://host:21")


def test_resolve_proxy_missing_hostname():
    with pytest.raises(ValueError, match="hostname"):
        utils.resolve_proxy("http://")


def test_resolve_proxy_socks5_allowed():
    assert utils.resolve_proxy("socks5://user:pass@host:1080") == "socks5://user:pass@host:1080"


def test_resolve_proxy_rejects_invalid_port():
    with pytest.raises(ValueError, match="port"):
        utils.resolve_proxy("http://host.example:70000")
