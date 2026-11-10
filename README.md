# UAR Copilot

User access review copilot. Deterministic rules engine (FastAPI) plus a React/Vite UI.
See the PRD for the roadmap; this repo currently implements Phase 0 (hardened prototype).

## Backend

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest
AUTH_DISABLED=true uvicorn backend.main:app --reload   # local dev only
```

| Variable | Required | Purpose |
| --- | --- | --- |
| `AUTH0_DOMAIN`, `AUTH0_AUDIENCE` | Yes (deployed) | Auth is fail-closed: without these, `/upload` returns 503; without a valid token, 401 |
| `AUTH_DISABLED` | No | `true` skips token checks. Local development only, never deployed |
| `CORS_ORIGINS` | Yes (deployed) | Comma-separated frontend origins, e.g. `https://uar-copilot.vercel.app`. `*` is ignored |
| `TERMINATION_GRACE_BUSINESS_DAYS` | No | Default `1`. Terminated-but-active within this window is `medium`, after it `high` |
| `LOG_LEVEL` | No | Default `INFO`. Logs are JSON and never contain row contents |

## Frontend (`frontend/uar-ui`)

Copy `.env.example` to `.env.local` and set `VITE_API_BASE_URL` and the `VITE_AUTH0_*` values
(the audience must match `AUTH0_AUDIENCE`). Then `npm ci && npm run dev`.

## Rules

Rules live in `backend/rule_registry.py` as versioned data. Operators:
`==, !=, >, >=, <, <=, in, not_in, contains, regex, days_since`.
Privileged roles are tiered by `backend/privileged_access_catalog.py`.

| Rule | Severity |
| --- | --- |
| `terminated_privileged_active` | critical |
| `terminated_active` (past grace period) | high |
| `privileged_access_critical` | high |
| `terminated_active_within_grace` | medium |
| `privileged_access_high` | medium |
| `privileged_access_medium` | low |
| `admin_role` (looks admin, not in catalog) | low |

Every issue carries `rule_version`, the `inputs` that triggered it, a `row_ref` and SOC 2 `control_refs`,
and the response includes column-mapping decisions and a SHA-256 of each uploaded file.
