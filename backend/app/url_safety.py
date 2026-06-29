"""URL-safety helpers shared by the assets cache and any future
internal fetcher that takes a URL the user controls.

Right now two surfaces accept user-supplied URLs and feed them into
``httpx`` / ``feedparser``:

  - ``Source.url`` itself (an RSS feed URL or a subreddit reference
    — both already validated by ``routes/sources.py`` before
    persistence, but the user's URL can still resolve to a
    loopback / private IP at fetch time)
  - Entry thumbnails (``entry.image_url``, scraped from RSS feeds or
    the source's homepage) and source favicons, both pulled via
    ``assets.py:_download`` and ``_pick_favicon_url``

Both surfaces are gated by the same IP-allowlist check here so a
LAN attacker can't use a feed or favicon URL to SSRF into
``127.0.0.1``, ``169.254.169.254`` (cloud metadata), or the local
``192.168.x.x`` / ``10.x.x.x`` range and pull secrets off the host.

Constraint:
  - "the IP is taken from the TCP peer only. X-Forwarded-For is
    ignored — it can be spoofed by any client and would let a LAN
    attacker claim a loopback identity."
  - "If you need to run behind a reverse proxy, terminate TLS at the
    proxy and have it speak to the backend on a private interface,
    or add explicit proxy-trust support (out of scope for the local
    bypass)."

So we resolve the host, and check every concrete address the
resolver returns. If any one of them is in a denied range, the URL
is rejected. ``httpx`` does its own DNS lookup; we don't try to
match the path it picks. When the allowlist is in effect, every
``getaddrinfo`` candidate must clear it.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Tuple

# Range to reject. Picked from RFC 6890 (special-purpose address
# registries) + the cloud metadata IPs worth pinning.
#
# Why these and not "block 0.0.0.0/8 and keep going": the goal is to
# make the LAN attacker's life hard, not to be a perfect firewall.
# An attacker who can spin up a public DNS record pointing at
# 127.0.0.1 can also point at our backend's public interface. We
# focus on the realistic local-network / metadata-service vectors
# that an attacker with control of the URL but not control of DNS
# can reach:
#
#   - 127.0.0.0/8     — loopback. The backend itself lives here.
#   - 0.0.0.0/8       — "any address". Hits the first bound interface.
#   - 10.0.0.0/8      — RFC 1918 private.
#   - 172.16.0.0/12   — RFC 1918 private (Docker default bridge is
#                       172.17.0.0/16, which falls in here).
#   - 192.168.0.0/16  — RFC 1918 private.
#   - 169.254.0.0/16  — link-local. The IPv4 cloud metadata IP
#                       (169.254.169.254) lands in here; on most
#                       Linux distros this range also includes the
#                       host's primary interface when no DHCP lease
#                       is available.
#   - 169.254.169.254 — AWS / GCP / Azure / DigitalOcean metadata
#                       endpoint (also covered by the 169.254.0.0/16
#                       line above, listed verbatim for clarity).
#   - 100.100.100.200 — Alibaba Cloud metadata. Outside the
#                       169.254/16 range, so listed separately.
#   - 192.0.0.0/24    — IETF protocol assignments (rare, but
#                       shouldn't be a fetch target).
#   - 192.0.2.0/24    — TEST-NET-1 documentation block.
#   - 198.18.0.0/15   — benchmark testing range (often used by some
#                       stub resolvers; not a real public host).
#   - 198.51.100.0/24 — TEST-NET-2 documentation.
#   - 203.0.113.0/24  — TEST-NET-3 documentation.
#   - 224.0.0.0/4     — multicast. Multicast sources aren't a real
#                       attack surface but they're not a normal
#                       fetch target either.
#   - 240.0.0.0/4     — reserved (broadcast + future use).
#   - ::1/128         — IPv6 loopback.
#   - fc00::/7        — IPv6 unique-local (RFC 4193). Docker's
#                       default IPv6 bridge lands here.
#   - fe80::/10       — IPv6 link-local.
#   - fe80::ec2::254  — AWS EC2 IPv6 metadata. Same range as the
#                       previous line.
#   - ::ffff:0:0/96   — IPv4-mapped IPv6. The IPv6 stack wraps
#                       loopback / private IPv4 in this prefix;
#                       without it an ``http://[::ffff:127.0.0.1]/``
#                       URL would slip past the IPv4 rules.
#
# Single IP allow/block: ``ipaddress.ip_network(x, strict=False)``.
_DENIED_NETWORKS: list[ipaddress._BaseNetwork] = [
    ipaddress.ip_network(net)
    for net in (
        "127.0.0.0/8",
        "0.0.0.0/8",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",
        "100.100.100.200/32",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
        "::ffff:0:0/96",
    )
]


def _ip_denied(ip: ipaddress._BaseAddress) -> bool:
    return any(ip in net for net in _DENIED_NETWORKS)


def resolve_addresses(host: str) -> list[ipaddress._BaseAddress]:
    """Resolve ``host`` to every concrete address ``getaddrinfo``
    returns.

    Skips entries that don't resolve to a valid IP (so a non-IP
    alias like ``localhost`` triggers the proper handling in
    ``check_url_safe``). Returns an empty list on resolution failure
    — the caller treats that as "URL unsafe" rather than as "URL safe"
    so a broken resolver doesn't accidentally let the request through.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return []
    out: list[ipaddress._BaseAddress] = []
    for info in infos:
        sockaddr = info[4]
        addr = sockaddr[0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        out.append(ip)
    # Dedup — multiple sockaddrs can resolve to the same ip.
    return list(dict.fromkeys(out))


def check_host_safe(host: str) -> Tuple[bool, str]:
    """Return ``(True, "")`` if every IP ``host`` resolves to is
    outside the denied ranges, ``(False, reason)`` otherwise.

    An empty / unresolved result is treated as unsafe: a host that
    doesn't resolve should be a 404, not a free pass.
    """
    if not host:
        return False, "host is empty"
    # Strip an IPv6 literal's brackets before checking (urlparse
    # already handled this for callers passing parsed URLs). Treat a
    # bare IPv6 the same way: anything that looks like an IP literal
    # goes through ``ip_denied`` directly without DNS, so an attacker
    # can't bypass the check via a stale cache or a custom resolver.
    bare = host.strip("[]").lower()
    if bare.startswith("::ffff:"):
        # IPv4-mapped IPv6 already covered by the ::ffff:0:0/96 line
        # in _DENIED_NETWORKS, but normalised here so the message is
        # readable.
        try:
            ip = ipaddress.ip_address(bare)
        except ValueError:
            return False, f"could not parse ip {bare!r}"
        if _ip_denied(ip):
            return False, f"host {bare!r} is in a denied range"
        return True, ""
    try:
        ip = ipaddress.ip_address(bare)
    except ValueError:
        # Hostname path — resolve and check every concrete IP.
        addresses = resolve_addresses(bare)
        if not addresses:
            return False, f"host {host!r} did not resolve to any IP"
        for ip in addresses:
            if _ip_denied(ip):
                return (
                    False,
                    f"host {host!r} resolves to denied address {ip}",
                )
        return True, ""
    # Bare IP literal: check directly.
    if _ip_denied(ip):
        return False, f"host {host!r} is in a denied range"
    return True, ""


def check_url_safe(url: str) -> Tuple[bool, str]:
    """Return ``(True, "")`` if URL is safe to fetch from the
    backend, ``(False, reason)`` otherwise.

    Scheme must be http or https (lets the caller pass through any
    URL — assets / RSS / favicon). Host must resolve (or be a literal
    IP) and every resolved address must clear the deny list.

    Callers should treat ``False`` as "skip the fetch" — never
    surface the reason to the end user, since the reason leaks which
    internal subnets are reachable.
    """
    from urllib.parse import urlparse  # local import; this module is on the import
                                       # path during config load and we don't
                                       # want a cycle through ip_safety.

    try:
        parsed = urlparse(url)
    except ValueError:
        return False, "url is not a valid URL"
    if parsed.scheme not in ("http", "https"):
        return False, f"scheme {parsed.scheme!r} is not http or https"
    if not parsed.hostname:
        return False, "url must include a host"
    return check_host_safe(parsed.hostname)


__all__ = [
    "check_url_safe",
    "check_host_safe",
    "resolve_addresses",
]
