# UAR Copilot: project memory

Read this first. It is the hand-off between working sessions: what exists, how it is deployed,
the decisions already made, and what comes next. Keep it current when something changes.

## Product
An agent that runs a full user access review (UAR) campaign for SOC 2 (CC6.1–CC6.3): ingest
access and HR data, match accounts to people, detect risk with deterministic rules, route items to
reviewers, record decisions, remediate, and export an auditor-ready evidence pack. Wedge: 50–500
person SaaS companies and MSPs/vCISOs. Guiding rule from the PRD: **the rules engine decides, the
LLM explains and orchestrates, a human approves.** Every finding must be reproducible without an
LLM; every action leaves an immutable, auditor-readable record.

The full PRD ("UAR Copilot — PRD & Technical Blueprint", Nov 10 2026) is in `docs/PRD.md`
(its embedded diagrams did not survive export). Its phases: 0 harden prototype → 1 MVP campaign → 2 GA (connectors, Slack/Teams,
auditor portal, MSP console, AWS) → 3 continuous access monitoring → 4 SOC 2 platform.

## Status (as of 2026-11-15)
**Phase 0: done** (PR #1). Fail-closed Auth0, CORS allowlist, pinned deps, tiered privileged
catalog in rules, grace-period rule, more operators, column-mapping transparency, tests, CI.

**Phase 1: core flow done and verified live by the user** (PRs #2–#5): a signed-in user created a
campaign, uploaded the sample HR roster and AWS access list, matched identities, ran analysis,
started the review and decided all 7 items on https://uar-copilot.vercel.app.

| Slice | PR | What |
| --- | --- | --- |
| 1 | #2 | Postgres schema (15 tables), Alembic, forced RLS per tenant, append-only `decision` and `evidence_event`, hash-chained evidence ledger, campaign state machine, CSV/XLSX ingest with snapshots |
| 2 + UI | #3 | Identity resolution with confidence + manual queue; campaign screens (React Router) |
| 3 | #4 | Analysis: findings + one review item per account entitlement, reviewer assignment |
| 4 | #5 | Reviewer queue: start review, keep/revoke/modify with justification rules, bulk keep (low/none only), self-review block, reassign, app owners |
| 5 | #7 | Sign-off (→ remediating, or closed if nothing changes), evidence pack ZIP (PDF via fpdf2, CSVs, raw uploads, manifest of SHA-256s, jsonl ledger, README on verifying), Campaigns as home page (`/` → `/campaigns`, quick scan at `/scan`), Auth0 sign-in error banner |
| — | #8, #9 | `/live-check` skill (Claude in Chrome); fix for "Missing Refresh Token" after Auth0 login expiry (refresh-token fallback + redirect to sign in). Auth0 API now has Allow Offline Access on |
| 6 | #10 | Reviewer authority (assigned reviewer decides; campaign owner = creator may decide instead with a recorded override reason; others 403; owner-only reassign/app owners/sign-off/remediation/close) and remediation (created at sign-off from revoke/modify; Jira-importable tickets CSV; ticket + mark done; verify by re-uploading an access list (`verification` snapshots, not campaign data); accept with reason; close → `closed`); pack adds remediations.csv and override columns |
| 7 | #11 | Shared workspaces: `workspace_member` (owner/admin/reviewer/auditor; invited→active), personal workspace auto-created on first sign-in (existing `user:<sub>` tenants backfilled as owners), invites by email accepted in-app (no email sent yet), several workspaces per user (MSP) chosen with the `X-Workspace-Id` header, role checks on every campaign endpoint, workspace owners/admins count as campaign managers; UI workspace switcher, invitation banner, Workspace settings page, role-aware screens |
| 8 | #12 | Connectors: `connection` table (migration 0006, RLS), credentials Fernet-encrypted with env `CONNECTOR_SECRET_KEY`, Google Workspace (service account + domain-wide delegation, read-only: users, suspended, last login, admin roles) and Jira Cloud (email + API token: users/app accounts with groups; creates remediation tickets). Workspace → Connections (owner/admin; add, test, remove); campaign Sources "Pull from a connection" (raw API JSON is the snapshot, `source.pulled` event); Remediation "Send to Jira". Catalog knows Google/Jira admin roles (rule set 2026.11.3) |

**Phase 1 remaining, in the recommended order:**
- B2. Tickets by email (Jira is done via the Jira connection; the CSV export remains).
- Invitation emails (today the owner tells people to sign in; needs an email provider, same as reminders).
- **Connectors, next ones** (decided 2026-11-15): Google Workspace and Jira Cloud are built (#12).
  Recommended order after them: an HR source through a unified HRIS API (Merge or Finch) or
  BambooHR directly, then Okta / Microsoft Entra ID, GitHub, AWS IAM Identity Center. Workday
  directly only when a larger customer needs it. Also: verify remediation by re-pulling from a
  connection (today verification needs an uploaded file), and expand Google admin roles assigned
  to groups (today they produce a warning).
- C. Agent recommendations: Claude drafts keep/revoke + rationale citing finding IDs; schema-validated.
- D. Reminders and escalation (email) to reviewers.
- Also from the PRD Phase 0 list, still open: restructure backend into an `app/` package (deferred;
  current Render start command `uvicorn main:app` from `backend/` must keep working).

## Architecture
- **Backend**: FastAPI in `backend/`. `main.py` (app, CORS, JSON logging, migrations on startup),
  `auth.py` (Auth0 JWT, fail closed; fetches email from `/userinfo` when the token lacks it),
  `rules.py` + `rule_registry.py` (versioned rules, `RULE_SET_VERSION`), `mapping.py` (column
  mapper), `privileged_access_catalog.py`, `db/models.py`, `db/session.py` (`tenant_session` sets
  `app.tenant_id` with SET LOCAL), `domain/` (`campaigns`, `evidence`, `ingest`, `identity`,
  `analysis`, `review` (authority, decisions, sign-off), `remediation`, `export` (evidence pack),
  `workspaces`, `connections`), `connectors/` (`base` httpx client + errors, `secrets` Fernet,
  `google_workspace`, `jira_cloud`), `api/campaigns.py` (all `/campaigns` routes),
  `api/workspaces.py` (`/workspaces`), `api/connections.py` (`/connections`).
  Schemas in `schemas/user_schema.py` (repo root). Imports always `backend.*`.
- **Migrations**: `backend/migrations/versions/0001..0006`. A new tenant table must enable and force
  RLS in its migration and be added to `RLS_TABLES` in `db/models.py` (not `TENANT_TABLES`). Run automatically at startup when
  `DATABASE_URL` is set (`RUN_MIGRATIONS=false` opts out). Create new ones with
  `cd backend && alembic revision --autogenerate --rev-id 000N -m "..."`, then `alembic check`.
- **Frontend**: React 19 + Vite + TS in `frontend/uar-ui/`. `src/App.tsx` (nav, routes, Quick scan
  page), `src/pages/Campaigns.tsx`, `src/pages/CampaignDetail.tsx` (sources, identities,
  analysis/review, evidence), `src/api.ts` (`useApi` adds the Auth0 token; shared types),
  `src/config.ts`. `vercel.json` rewrites for deep links.
- **Tenancy**: a workspace is a tenant. `domain/workspaces.py` resolves the request's workspace from
  `X-Workspace-Id` (default: personal `user:<sub>`) and the caller's role from `workspace_member`
  (no RLS on that table; matched by Auth0 sub, or by verified email for pending invites). The role is
  put in a contextvar by `ctx.activate()` inside `_load` (FastAPI runs the dependency and endpoint in
  different threads). `org_id` claims still map to an `org:<id>` workspace (role admin).
  Local dev: `AUTH_DISABLED=1` plus `LOCAL_DEV_SUB` / `LOCAL_DEV_EMAIL` to act as different users.
- **Rules (2026.11.3)**: terminated_privileged_active (critical), terminated_active past grace
  (high), privileged_access_critical (high), terminated_active_within_grace (medium),
  privileged_access_high (medium), external_account_active (medium), privileged_access_medium (low),
  dormant_account >90 days (low), admin_role uncatalogued (low).

## Deployment
- **Frontend**: Vercel project `uar-copilot` (team `hfrank456s-projects`, id
  `prj_WDR0NIg1VmxPtd9HVO3MGwxas9qW`), root `frontend/uar-ui`, https://uar-copilot.vercel.app.
  Env: `VITE_AUTH0_DOMAIN`, `VITE_AUTH0_CLIENT_ID`, `VITE_AUTH0_AUDIENCE`, `VITE_API_BASE_URL`.
- **Backend**: Render web service `uar-copilot` (`srv-d7a5jrbuibrs73fqkiu0`, workspace
  `tea-d7a5bu3uibrs73dgjn10`, region virginia), rootDir `backend`, start
  `uvicorn main:app --host 0.0.0.0 --port 10000`, auto-deploys from `main`,
  https://uar-copilot.onrender.com. Env: `AUTH0_DOMAIN`, `AUTH0_AUDIENCE`,
  `CORS_ORIGINS=https://uar-copilot.vercel.app`, `DATABASE_URL` (internal URL),
  `CONNECTOR_SECRET_KEY` (Fernet key for connection credentials; set 2026-11-15; never change
  it without re-adding connections).
- **Database**: Render Postgres `uar-copilot-db` (`dpg-dbpr4o56e07s73b82po0-a`), **free plan,
  expires 2026-12-11**; upgrade before real customer data. External connections are blocked (empty
  IP allowlist), so it can't be queried from a sandbox; check Render logs instead.
- **Auth0**: tenant `dev-pzmiocjfeo5mjqn2.us.auth0.com`; SPA "UAR Copilot Product"
  (client id `quAhlmyQnyqPa1NLIsDlnnary4oJNBwm`, public); API identifier `https://uar-copilot-api`.
  The SPA needed **User-Delegated Access** granted to the API (fixed 2026-11-10). Allowed
  callback/logout/web origins: the Vercel URL and `http://localhost:5173`.
- Render and Vercel connectors work from Claude sessions (Vercel needed a one-time team-access
  approval link).

## Open items for the user
- **Rotate the database password**: it was pasted into chat on 2026-11-11. Reset it in Render
  (database → Connections), then update `DATABASE_URL` on the backend service.
- Upgrade the free database before 2026-12-11.
- PRD open questions still undecided: lead market (direct vs MSP first), first design partners and
  their HRIS/IdP (drives connector order), whether a CPA firm will review the evidence pack format,
  pricing unit.

## How to work in this repo
- Workflow so far: one slice per PR on branch `claude/admiring-faraday-bq46yw`, restarted from
  `origin/main` after each merge (`git fetch origin main && git checkout -B <branch> origin/main`).
  Normal fast-forward pushes; no force-push. The user merges PRs themselves; Claude watches CI.
- Tests: `python -m pytest`. Database tests need an empty DB owned by a **non-superuser** (superusers
  bypass RLS): `TEST_DATABASE_URL=postgresql://uar_app:app@127.0.0.1:5433/uar_test`. A local
  cluster was run with `/usr/lib/postgresql/16/bin/pg_ctl` on port 5433; it does not survive a new
  sandbox, so recreate role `uar_app` and databases `uar`/`uar_test` owned by it.
- Frontend checks: `cd frontend/uar-ui && npm ci && npm run lint && npm run build`.
- Connector tests use `httpx.MockTransport` through `backend.connectors.base.transport_factory`;
  the sandbox can't reach Google or Atlassian. For a browser check of connectors, start the backend
  from a script that sets `transport_factory` to the tests' handlers (`tests/test_connectors.py`).
- Browser check: run backend with `AUTH_DISABLED=1 DATABASE_URL=...` on :8000 and
  `VITE_API_BASE_URL=http://localhost:8000 npx vite` with empty `VITE_AUTH0_*`, then drive with
  Playwright. This caught a real bug (CORS lacked PUT).
- Sample data for a full campaign: `data/sample_hr_roster.csv`, `data/sample_app_access_aws.csv`,
  `data/sample_app_access_github.csv`. With all three: 11 accounts, 4 queued identities, 12 review
  items (1 critical: terminated Jordan Kim with AWS admin). `data/sample_app_access_aws_after_remediation.csv`
  is a later AWS export (Jordan removed, contractor still present) for testing verification.
- **Live checks**: cloud sessions can't reach the live backend (sandbox blocks `onrender.com`) or the
  user's browser. After each merge that changes something user-visible, check the Render deploy
  logs, then tell the user the exact command to run in a **Claude desktop app session with Claude in
  Chrome**: `/live-check` for the full end-to-end check, or `/live-check <specific action>` for one
  change (skill in `.claude/skills/live-check/SKILL.md`; keep its expected results in sync when
  behaviour changes). The user signs in to Auth0 themselves; Claude never handles their password.
- Keep user-facing explanations plain; the user is non-technical about infra and prefers exact
  click-by-click steps for dashboards (Auth0, Render, Vercel, GitHub).
