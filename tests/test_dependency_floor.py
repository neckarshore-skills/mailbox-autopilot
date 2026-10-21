"""Security floors for runtime dependencies. A lock change that drops below one fails here."""

from importlib.metadata import version


def _v(s):
    return tuple(int(p) for p in s.split("."))


def test_certifi_has_no_compromised_emsign_roots():
    """certifi < 2026.10.19 bundles emSign roots whose intermediate CA keys were
    compromised (Dependabot alert 11). requests uses it for the HTTPS unsubscribe."""
    assert _v(version("certifi")) >= (2026, 10, 19)
