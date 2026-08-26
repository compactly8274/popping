# Backend Tests

## Test Categories

### Integration Tests (`test_*.py`)
Standard backend integration tests that exercise API endpoints, DB
state, and scoring logic. These require a database (via the `conftest.py`
fixtures) and are the bulk of the test suite.

### Source-Code Guards (`test_frontend_*.py`, `test_slice*.py`)
These are **Python regex checks on TypeScript/React source files** —
not backend integration tests. They live in `backend/tests` because
the project's test runner is `pytest` (no separate JS/TS test runner is
configured). They're marked `@pytest.mark.no_db` so they skip DB
fixtures and can run in any environment.

The `frontend` prefix in some filenames refers to which UI slice the
test was written for, not that they test frontend code in a browser.
The real frontend verification is the TypeScript build in CI; these
guards are a belt-and-suspenders check that the source patterns the
backend relies on (e.g. `useEffect` deps, `React.memo` wrapping) are
preserved across refactors.

If a dedicated JS/TS test runner (Vitest, Jest) is added in the future,
these guards should be migrated to that runner and removed from here.

### Test Discovery

All `test_*.py` files are discovered by pytest. The `@pytest.mark.no_db`
marker on source-code guards ensures they don't trigger the DB session
fixtures, so they can run in CI without a database.