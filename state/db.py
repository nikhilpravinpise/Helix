"""Engine and session factory for the incident state store.

The store is the host Postgres from infra/postgres-compose.yml (D3), pointed
at by HELIX_DATABASE_URL - a full SQLAlchemy URL, so tests can substitute
SQLite without touching config shape.

`session_scope()` is the one way callers should hold a session: it commits
on clean exit and rolls back on any exception, which is what makes
"transition row and status update land in the same transaction" hold for
every code path (state/machine.py only ever flushes, never commits).
"""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from helix.config import get_settings

# Engines are pooled objects; cache one per URL so repeated session_scope()
# calls share a pool, while a changed HELIX_DATABASE_URL (tests) still gets
# a fresh engine rather than a stale cached one.
_engines: dict[str, Engine] = {}


def get_engine(url: str | None = None) -> Engine:
    """Return the pooled engine for `url` (default HELIX_DATABASE_URL)."""
    url = url or get_settings().database_url
    engine = _engines.get(url)
    if engine is None:
        engine = create_engine(url)
        _engines[url] = engine
    return engine


@contextmanager
def session_scope(engine: Engine | None = None) -> Iterator[Session]:
    """Transactional session: commit on clean exit, rollback on exception,
    always closed. `expire_on_commit=False` so objects read or created
    inside the block stay usable after it exits."""
    session = Session(engine or get_engine(), expire_on_commit=False)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
