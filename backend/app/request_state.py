"""Request-scoped helpers.

Small bag of helpers that bridge between the FastAPI app state
(populated in ``main.py``'s lifespan) and request handlers or
background jobs. We don't want to import ``app.main`` from
every route — that creates a circular import — so this module holds
the bridge.

The notifier is built once at startup (in lifespan) and shared by
all scheduler jobs + the manual brief endpoint.
"""

from __future__ import annotations

from typing import Optional

from app.notify import Notifier

_notifier: Optional[Notifier] = None


def set_notifier(notifier: Optional[Notifier]) -> None:
    """Called by the lifespan handler in ``app.main``. Stores the
    process-wide notifier reference."""
    global _notifier
    _notifier = notifier


def current_notifier() -> Optional[Notifier]:
    """Return the notifier, or None if no backend is configured."""
    return _notifier


__all__ = ["set_notifier", "current_notifier"]