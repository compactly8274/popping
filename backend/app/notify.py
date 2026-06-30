"""Notification backends.

Phase 4 ships two of them:

  - Apprise   — preferred. One opaque URL (``APPRISE_URL``) covers 100+
                services (Pushover, Telegram, Discord, Slack, email,
                ntfy, Gotify, …). Apprise dispatches server-side.
  - Pushover  — direct fallback. Two env vars (``PUSHOVER_USER_KEY``,
                ``PUSHOVER_APP_TOKEN``). Plain ``httpx`` POST.

``build_notifier()`` picks Apprise when ``APPRISE_URL`` is set, else
Pushover when both Pushover vars are set, else ``None``. ``send()``
is best-effort: failures are logged and swallowed so a broken
notification backend can't take the app down.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.config import settings

logger = logging.getLogger("popping.notify")


def _scrub_url_for_log(raw: str) -> str:
    """Return ``raw`` with any userinfo (``user:pass@``) stripped.

    Apprise URLs are opaque credential-bearing strings
    (``pover://userkey/appkey``, ``tgram://bottoken/chatid``,
    ``mailto://user:pass@host``, …). Logging the raw URL writes the
    tokens to whatever log aggregator captures the message — a
    sequence number or stdout-shipper with broad read access is
    enough to leak them. Replace userinfo with ``***`` and re-emit
    so operators still see ``scheme://***@host/path`` and can
    distinguish "auth BadURL" from "network timeout" at a glance.
    """
    if not raw:
        return raw
    try:
        parts = urlsplit(raw)
    except ValueError:
        # Malformed URL — return the raw string. Nothing to scrub on
        # a parse failure anyway; logging parsers don't crash on
        # garbage and the operator sees the same thing they'd see
        # for any other broken input.
        return raw
    if not parts.netloc or "@" not in parts.netloc:
        return raw
    # ``netloc`` for ``user:pass@host:port`` is ``user:pass@host:port``.
    # Split on the rightmost ``@`` so an IPv6 literal (``[::1]@host``)
    # doesn't get its brackets mistaken for the userinfo boundary.
    userinfo, _, hostinfo = parts.netloc.rpartition("@")
    return urlunsplit(
        (parts.scheme, f"***@{hostinfo}", parts.path, parts.query, parts.fragment)
    )


class Notifier(ABC):
    """One notification backend."""

    name: str

    @abstractmethod
    async def send(self, *, title: str, body: str, url: Optional[str] = None) -> None:
        """Push one notification. Implementations must not raise on
        transport / API errors — log and return."""
        raise NotImplementedError


class AppriseNotifier(Notifier):
    """Thin wrapper over the ``apprise`` library.

    The URL is opaque (``pover://userkey/appkey``, ``tgram://…``,
    ``mailto://…``, ``ntfy://topic@host``, …). Apprise decides what to
    do with it; we just hand over title + body.
    """

    name = "apprise"

    def __init__(self, apprise_url: str) -> None:
        self._url = apprise_url

    async def send(self, *, title: str, body: str, url: Optional[str] = None) -> None:
        # Apprise is sync — ``notify`` opens its own short-lived HTTP
        # connections. Running it inline in the event loop would block;
        # push it to the default executor so the scheduler keeps moving.
        import asyncio

        from apprise import Apprise

        def _do() -> bool:
            ap = Apprise()
            if not ap.add(self._url):
                logger.error("apprise: invalid URL '%s'", _scrub_url_for_log(self._url))
                return False
            kwargs: dict = {"title": title, "body": body}
            if url:
                # Apprise treats ``url`` as the click-through link for
                # backends that support it. Optional; Pushover does.
                kwargs["url"] = url
            return ap.notify(**kwargs)

        try:
            ok = await asyncio.get_running_loop().run_in_executor(None, _do)
            if not ok:
                logger.warning(
                    "apprise: notify returned False (URL=%s)",
                    _scrub_url_for_log(self._url),
                )
        except Exception:
            logger.exception(
                "apprise: send failed (URL=%s)", _scrub_url_for_log(self._url)
            )


class PushoverNotifier(Notifier):
    """Direct Pushover POST. Used when ``APPRISE_URL`` is unset.

    Pushover's free API is at https://api.pushover.net/1/messages.json.
    The ``url`` field is optional — when present, the device shows a
    click-through link on the notification.
    """

    name = "pushover"
    _ENDPOINT = "https://api.pushover.net/1/messages.json"

    def __init__(self, user_key: str, app_token: str) -> None:
        self._user = user_key
        self._token = app_token

    async def send(self, *, title: str, body: str, url: Optional[str] = None) -> None:
        # Pushover enforces a 512-char title and 1024-char body — clip
        # generously below to avoid surprise 4xx.
        payload = {
            "token": self._token,
            "user": self._user,
            "title": title[:250],
            "message": body[:4000],
        }
        if url:
            # Strip control bytes (CR/LF/NUL/etc.) before the
            # length cap. httpx form-encodes the field so a CRLF
            # in the URL can't break out of the POST body itself,
            # but Pushover's server-side URL rendering has been
            # observed to interpret the *decoded* URL with newlines
            # in unexpected ways (the URL appears intact in the
            # notification but the in-app click handler falls open
            # on an empty target). Defense-in-depth strip.
            sanitized = "".join(c for c in url if 0x20 <= ord(c) < 0x7f)
            payload["url"] = sanitized[:500]
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
                resp = await client.post(self._ENDPOINT, data=payload)
            if resp.status_code >= 300:
                logger.warning("pushover: %s %s", resp.status_code, resp.text[:200])
        except Exception:
            logger.exception("pushover: send failed")


def build_notifier() -> Notifier | None:
    """Pick a backend from env. ``None`` means "no notifications" — callers
    must handle the absence (just log + skip)."""
    if settings.apprise_url:
        return AppriseNotifier(settings.apprise_url)
    if settings.pushover_user_key and settings.pushover_app_token:
        return PushoverNotifier(settings.pushover_user_key, settings.pushover_app_token)
    return None


def notifier_status() -> dict:
    """Human-readable state for the Drawer chip. Doesn't leak secrets."""
    if settings.apprise_url:
        # Show the scheme only — full URL would expose tokens in logs.
        scheme = settings.apprise_url.split("://", 1)[0] if "://" in settings.apprise_url else "apprise"
        return {"configured": True, "backend": "apprise", "scheme": scheme}
    if settings.pushover_user_key and settings.pushover_app_token:
        return {"configured": True, "backend": "pushover", "scheme": "pushover"}
    return {"configured": False, "backend": None, "scheme": None}


__all__ = [
    "Notifier",
    "AppriseNotifier",
    "PushoverNotifier",
    "build_notifier",
    "notifier_status",
]