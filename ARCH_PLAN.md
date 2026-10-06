# ARCH_PLAN

## Objective
Implement three critical safeguards against HTML/JS hallucinations for the `assassin` repository.

## Tasks

### 1. Local Assets
- Create directory `app/static/` if it does not exist.
- Download `htmx.min.js` (v2.0.4) into `app/static/htmx.min.js`. (You can download it from `https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js`).
- Update the FastAPI application instance to serve static files. Mount it at `/static`.
  - Locate where FastAPI `app` is defined (likely `app/main.py` or similar).
  - Add `from fastapi.staticfiles import StaticFiles`.
  - Add `app.mount("/static", StaticFiles(directory="app/static"), name="static")`.
- Update `app/api/dashboard.py` (or wherever the dashboard HTML is returned) to source HTMX locally (`/static/htmx.min.js`) instead of using the external CDN. Remove any `integrity` attributes on the script tag.

### 2. Update AGENTS.md
- Add a strict invariant to `AGENTS.md` forbidding agents from hallucinating cryptographic hashes (SRI, SHA, MD5) or UUIDs for frontend assets.
- Mandate that they must either serve assets locally from `app/static/`, compute hashes programmatically, or omit them.
(Note: Check if this invariant is already in `AGENTS.md`. If so, ensure it matches the wording.)

### 3. Playwright E2E Tests
- Add `pytest-playwright` to the project using `uv add pytest-playwright`.
- Update `.github/workflows/ci.yml` (if it exists) to execute `uv run playwright install --with-deps` before the `pytest` step. *CRITICAL!*
- Write an E2E test `tests/test_e2e_dashboard.py` that:
  - Spins up the FastAPI server in a background thread or using a test client if possible, but Playwright requires an actual HTTP server. Consider using `multiprocessing` or `threading` with `uvicorn`, or use `TestClient` if Playwright supports it (it usually needs a real port).
  - Use Playwright to open the dashboard, click the 'Analyze' button.
  - Assert that the HTMX DOM swap occurs (wait for a specific element to appear or change).
- Ensure all CI gates pass: `uv run ruff check .`, `uv run pytest --cov=app --cov-fail-under=90 -v`, `mutmut run`.

## Execution Steps
1. Create a new branch `safeguard-htmx`.
2. Implement step 1.
3. Implement step 2.
4. Implement step 3.
5. Run tests locally.
6. Commit and open PR.
