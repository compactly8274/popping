"""``GET /api/interactions/recent`` type filtering (Settings -> History).

The route used to append ``dwell`` to the types filter before also
excluding it, so a request with no ``types`` became ``type IN (dwell)
AND type NOT IN (dwell)`` and History was always empty. Now:

- no types: every type except ``dwell`` and ``view`` (views get
  frequent once seen-on-scroll lands);
- explicit types pass through, except ``dwell``, which is never listed.
"""

from __future__ import annotations

from app.auth.deps import ANONYMOUS_USER_ID
from app.models import Interaction
from factories import make_entry, make_source


async def _seed(db_session) -> None:
    source = await make_source(db_session, "history_feed")
    entry = await make_entry(db_session, source, "A story")
    for kind in ("never", "bookmark", "dwell", "view"):
        db_session.add(Interaction(entry_id=entry.id, user_id=ANONYMOUS_USER_ID, type=kind))
    await db_session.commit()


async def _types(app_client, query: str = "") -> tuple[set[str], int]:
    resp = await app_client.get(f"/api/interactions/recent{query}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return {item["type"] for item in body["items"]}, body["total"]


async def test_no_types_lists_everything_but_dwell_and_view(app_client, db_session):
    await _seed(db_session)
    kinds, total = await _types(app_client)
    assert kinds == {"never", "bookmark"}
    assert total == 2


async def test_no_types_grouped_by_entry_is_not_empty(app_client, db_session):
    await _seed(db_session)
    kinds, total = await _types(app_client, "?group_by=entry")
    assert len(kinds) == 1 and kinds <= {"never", "bookmark"}
    assert total == 1


async def test_explicit_view_returns_views(app_client, db_session):
    await _seed(db_session)
    kinds, total = await _types(app_client, "?types=view")
    assert kinds == {"view"}
    assert total == 1


async def test_explicit_types_pass_through(app_client, db_session):
    await _seed(db_session)
    kinds, _ = await _types(app_client, "?types=view,never")
    assert kinds == {"view", "never"}


async def test_explicit_dwell_returns_nothing(app_client, db_session):
    await _seed(db_session)
    kinds, total = await _types(app_client, "?types=dwell")
    assert kinds == set()
    assert total == 0
