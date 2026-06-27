"""Source plugin system.

Each source lives in its own module under `app/sources/`. Plugins register
themselves via the `@register_source` decorator imported from this package.
Importing `app.sources` runs the per-plugin module imports, which registers
each plugin as a side effect — so discovery is automatic and dropping a new
file in `app/sources/<name>.py` is enough to make it available at runtime
(no central registry to update).

Plugin contract (see `base.py`):
    class MySource(SourcePlugin):
        name = "my_source"
        type = "rss"
        category = "news"
        url = "https://..."
        refresh_interval_seconds = 3600

        async def fetch(self) -> list[dict]:
            ...

Calling convention:
    cls = list_sources()["my_source"]          # get the class
    plugin = cls()                              # instantiate
    raw_items = await plugin.fetch()            # fetch raw items
    # `normalize()` is applied by the scheduler, not the plugin.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.sources.base import SourcePlugin


_registry: dict[str, type[SourcePlugin]] = {}


def register_source(
    cls: type["SourcePlugin"],
) -> type["SourcePlugin"]:
    """Class decorator that registers a SourcePlugin subclass.

    Validation is light on purpose: the plugin author owns their config.
    Duplicate names raise so the failure is loud (two plugins claiming the
    same name is a bug, not something to silently override).
    """
    if not getattr(cls, "name", None):
        raise ValueError(f"{cls.__name__} must define a class-level `name`")
    if cls.name in _registry:
        raise ValueError(f"duplicate source name: {cls.name!r}")
    _registry[cls.name] = cls
    return cls


def list_sources() -> dict[str, type["SourcePlugin"]]:
    """Return a copy of the registry. Safe to iterate; safe to call early."""
    return dict(_registry)


def get_source(name: str) -> type["SourcePlugin"]:
    """Resolve a registered plugin class by name. Raises KeyError if unknown."""
    return _registry[name]


# --- Side-effect imports: each plugin module self-registers on import -------

# This is the only place we enumerate the built-in plugins. Adding a new
# plugin means adding one line here AND dropping the file in this package.
# (We could scan the directory with pkgutil, but explicit is cheaper to
# debug than implicit and the list stays short.)
from app.sources import rss  # noqa: F401, E402