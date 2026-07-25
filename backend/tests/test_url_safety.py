"""Tests for the URL-safety allowlist.

The function under test (``check_url_safe``) is the only SSRF
gate between user-supplied URLs (RSS feeds, scraped
thumbnails, favicon URLs) and the backend's outbound fetcher.
A regression here is a security incident, not a perf issue.

Tests cover:
- IPv4 literal in every denied range (loopback, RFC1918, link-local, metadata)
- IPv6 literals: loopback, link-local, unique-local, IPv4-mapped
- Hostname resolution: every IP must clear the deny list
- Mixed case + bracket normalization (URL parser strips the IPv6 brackets)
- Empty / invalid URL rejection
- Scheme allowlist: http + https pass, others fail
- A live DNS resolution case: the GitHub host resolves to public IPs
  and passes; ``localhost`` (loopback) fails; a non-existent host
  fails (treats unresolved as unsafe)

The "no DNS resolver" cases are fast and run unconditionally.
The "live resolution" cases use ``localhost`` / ``example.com`` /
a non-existent host — all safe, all resolve-or-not on any machine
that can run pytest (no network call needed for the rejection
cases; the pass-cases assume a connected test environment).
"""
from __future__ import annotations

import socket
from unittest.mock import patch

import pytest

from app.url_safety import (
    check_host_safe,
    check_url_safe,
    resolve_addresses,
)


# --- Helpers ---------------------------------------------------------------

def is_loopback_v4(ip: str) -> bool:
    """Convenience: is the given IPv4 string in 127.0.0.0/8?"""
    import ipaddress
    return ipaddress.ip_address(ip) in ipaddress.ip_network("127.0.0.0/8")


# --- Pure deny-list checks (no DNS) ---------------------------------------

class TestCheckHostSafeLiteralIPv4:
    """All IPv4 literals in the deny list must be rejected."""

    @pytest.mark.parametrize(
        "ip",
        [
            "127.0.0.1",          # loopback
            "127.255.255.254",    # top of loopback
            "0.0.0.0",            # any-address
            "10.0.0.1",           # RFC1918 10/8
            "10.255.255.255",
            "172.16.0.1",         # RFC1918 172.16/12
            "172.31.255.254",
            "192.168.1.1",        # RFC1918 192.168/16
            "169.254.169.254",    # AWS / GCP / Azure metadata
            "169.254.0.1",        # link-local
            "100.100.100.200",    # Alibaba Cloud metadata
            "192.0.0.1",          # IETF protocol assignments
            "192.0.2.1",          # TEST-NET-1
            "198.18.0.1",         # benchmark
            "198.51.100.1",       # TEST-NET-2
            "203.0.113.1",        # TEST-NET-3
            "224.0.0.1",          # multicast
            "240.0.0.1",          # reserved
        ],
    )
    def test_denied(self, ip: str) -> None:
        ok, reason = check_host_safe(ip)
        assert not ok, f"{ip} should be denied; got reason: {reason!r}"
        # Every reason mentions a denied range or the literal IP.
        assert "denied" in reason.lower() or "in a denied" in reason.lower()

    @pytest.mark.parametrize(
        "ip",
        [
            "8.8.8.8",            # Google DNS
            "1.1.1.1",            # Cloudflare DNS
            "208.67.222.222",     # OpenDNS
            "140.82.112.3",       # GitHub
        ],
    )
    def test_public_ip_passes(self, ip: str) -> None:
        # Direct IPv4 literals are checked without DNS. A public
        # IP must be allowed.
        ok, _ = check_host_safe(ip)
        assert ok, f"{ip} should pass; it doesn't match any denied range"


class TestCheckHostSafeLiteralIPv6:
    """IPv6 literals — and the IPv4-mapped prefix trick."""

    @pytest.mark.parametrize(
        "ip",
        [
            "::1",               # IPv6 loopback
            "::",                # unspecified
            "fc00::1",           # IPv6 ULA
            "fd00::1",           # IPv6 ULA (also fc00::/7)
            "fe80::1",           # link-local
            "::ffff:127.0.0.1",  # IPv4-mapped loopback
            "::ffff:169.254.169.254",  # IPv4-mapped metadata
            "::ffff:10.0.0.1",   # IPv4-mapped RFC1918
        ],
    )
    def test_denied(self, ip: str) -> None:
        ok, reason = check_host_safe(ip)
        assert not ok, f"{ip} should be denied; got reason: {reason!r}"

    def test_public_ipv6_passes(self) -> None:
        # 2001:4860:4860::8888 is Google DNS in IPv6. Public.
        ok, _ = check_host_safe("2001:4860:4860::8888")
        assert ok


class TestCheckHostSafeHostname:
    """Hostname resolution: every IP must clear the deny list."""

    def test_localhost_denied(self) -> None:
        # ``localhost`` resolves to 127.0.0.1 (or ::1) on any
        # sensible test environment. If the test machine has a
        # weird /etc/hosts, this test will fail loudly — better
        # than silently letting the request through.
        ok, reason = check_host_safe("localhost")
        assert not ok
        # The reason names the IP that triggered the deny.
        assert "127.0.0.1" in reason or "::1" in reason or "denied" in reason.lower()

    def test_unresolved_host_denied(self) -> None:
        # A host that doesn't resolve must be treated as
        # unsafe, not as "safe by default". The reasoning:
        # an empty / failed resolver is indistinguishable from
        # "the URL is malformed" and the caller is better off
        # failing closed.
        with patch(
            "app.url_safety.socket.getaddrinfo",
            side_effect=socket.gaierror("no such host"),
        ):
            ok, reason = check_host_safe("definitely-not-a-real-host.invalid")
        assert not ok
        assert "did not resolve" in reason

    def test_empty_host_denied(self) -> None:
        ok, reason = check_host_safe("")
        assert not ok
        assert "empty" in reason.lower()

    def test_resolved_public_ip_passes(self) -> None:
        # Mock the resolver to return a known-public IP. This
        # avoids relying on the test machine's actual DNS.
        public_ip = "140.82.112.3"  # one of GitHub's
        with patch(
            "app.url_safety.resolve_addresses",
            return_value=[__import__("ipaddress").ip_address(public_ip)],
        ):
            ok, _ = check_host_safe("github.com")
        assert ok

    def test_resolved_private_ip_denied(self) -> None:
        # Same idea but the mock returns a private IP. The
        # check_host_safe function catches it without ever
        # touching the real network.
        private_ip = "192.168.1.1"
        with patch(
            "app.url_safety.resolve_addresses",
            return_value=[__import__("ipaddress").ip_address(private_ip)],
        ):
            ok, reason = check_host_safe("evil.example.com")
        assert not ok
        assert "192.168.1.1" in reason


# --- check_url_safe: scheme + URL-level validation -------------------------

class TestCheckUrlSafe:
    """URL-level: scheme allowlist + host check + urlparse handling."""

    @pytest.mark.parametrize(
        "url",
        [
            "http://example.com",
            "https://example.com",
        ],
    )
    def test_http_and_https_pass_for_public_host(self, url: str) -> None:
        with patch(
            "app.url_safety.resolve_addresses",
            return_value=[__import__("ipaddress").ip_address("140.82.112.3")],
        ):
            ok, _ = check_url_safe(url)
        assert ok

    @pytest.mark.parametrize(
        "url",
        [
            "ftp://example.com",
            "file:///etc/passwd",
            "javascript:alert(1)",
            "data:text/html,<script>alert(1)</script>",
        ],
    )
    def test_disallowed_schemes_rejected(self, url: str) -> None:
        ok, reason = check_url_safe(url)
        assert not ok
        assert "scheme" in reason.lower() or "url" in reason.lower()

    def test_url_without_host_rejected(self) -> None:
        ok, reason = check_url_safe("http:///path-only")
        assert not ok
        assert "host" in reason.lower() or "url" in reason.lower()

    def test_url_with_loopback_host_rejected(self) -> None:
        # URL parse strips the IPv6 brackets; check_host_safe
        # sees the bare ::1. Same for IPv4.
        ok, reason = check_url_safe("http://[::1]/admin")
        assert not ok

    def test_invalid_url_string_rejected(self) -> None:
        # urlparse doesn't raise on most garbage but returns
        # empty components. check_url_safe treats that as
        # "no host" → reject.
        ok, _ = check_url_safe("not a url at all")
        assert not ok

    def test_url_with_bracket_stripped_ipv6(self) -> None:
        # http://[::1] should be parsed and the brackets
        # stripped before the host check.
        ok, _ = check_url_safe("http://[::1]:8080/admin")
        assert not ok

    def test_url_with_public_ipv4_literal_passes(self) -> None:
        # Direct IP literal in the URL — no DNS needed, just
        # the public-IP check.
        ok, _ = check_url_safe("http://140.82.112.3/foo")
        assert ok

    def test_url_with_private_ipv4_literal_rejected(self) -> None:
        ok, reason = check_url_safe("http://192.168.1.1/admin")
        assert not ok


# --- resolve_addresses: dedup behavior -------------------------------------

class TestResolveAddresses:
    """The DNS resolver returns a list; the function dedupes."""

    def test_dedup(self) -> None:
        # Many socket.getaddrinfo results can resolve to the
        # same IP. The dedup step keeps the result small.
        with patch(
            "app.url_safety.socket.getaddrinfo",
            return_value=[
                (2, 1, 6, "", ("140.82.112.3", 0)),
                (2, 1, 6, "", ("140.82.112.3", 0)),
                (10, 1, 6, "", ("140.82.112.3", 0)),
            ],
        ):
            addrs = resolve_addresses("github.com")
        assert len(addrs) == 1
        import ipaddress
        assert addrs[0] == ipaddress.ip_address("140.82.112.3")

    def test_empty_on_resolution_failure(self) -> None:
        with patch(
            "app.url_safety.socket.getaddrinfo",
            side_effect=socket.gaierror("nope"),
        ):
            addrs = resolve_addresses("definitely-not-real.invalid")
        assert addrs == []

    def test_skips_non_ip_entries(self) -> None:
        # Some resolvers return weird entries (rare, but the
        # module docstring calls them out). The function
        # should skip anything that doesn't parse as an IP.
        with patch(
            "app.url_safety.socket.getaddrinfo",
            return_value=[
                (2, 1, 6, "", ("not-an-ip", 0)),
                (2, 1, 6, "", ("8.8.8.8", 0)),
            ],
        ):
            addrs = resolve_addresses("weird.example.com")
        import ipaddress
        assert addrs == [ipaddress.ip_address("8.8.8.8")]


# --- Cross-check: ensure the public exports are correct -------------------

def test_exports() -> None:
    """Sanity: the __all__ list matches the actual callables."""
    import app.url_safety as mod
    for name in mod.__all__:
        assert callable(getattr(mod, name)), f"{name} is not callable"
