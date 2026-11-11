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

## Status (as of 2026-11-11)
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

**Phase 1 remaining, in the recommended order** (user has not yet chosen; last recommendation was A):
- A. **Evidence pack export** (PDF + CSV bundle: snapshots + hashes, mappings, findings, decisions with
  actor/time, chain verification, campaign sign-off) **plus two UX fixes from user testing**:
  1. Make Campaigns the home page; the old Quick scan page confused the user (they tried to upload
     all three files there). Label Quick scan as a separate one-off tool.
  2. Show Auth0's `?error=...&error_description=...` on the page after a failed sign-in (today it
     is silently dropped).
- B. Remediation: tickets (email/Jira) for revoke/modify, states remediating → verifying → closed.
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
  `analysis`, `review`, `tenants`), `api/campaigns.py` (all `/campaigns` routes).
  Schemas in `schemas/user_schema.py` (repo root). Imports always `backend.*`.
- **Migrations**: `backend/migrations/versions/0001..0003`. Run automatically at startup when
  `DATABASE_URL` is set (`RUN_MIGRATIONS=false` opts out). Create new ones with
  `cd backend && alembic revision --autogenerate --rev-id 000N -m "..."`, then `alembic check`.
- **Frontend**: React 19 + Vite + TS in `frontend/uar-ui/`. `src/App.tsx` (nav, routes, Quick scan
  page), `src/pages/Campaigns.tsx`, `src/pages/CampaignDetail.tsx` (sources, identities,
  analysis/review, evidence), `src/api.ts` (`useApi` adds the Auth0 token; shared types),
  `src/config.ts`. `vercel.json` rewrites for deep links.
- **Tenancy**: Auth0 `org_id` claim → tenant; otherwise a personal tenant per user (`user:<sub>`).
- **Rules (2026.11.2)**: terminated_privileged_active (critical), terminated_active past grace
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
  `CORS_ORIGINS=https://uar-copilot.vercel.app`, `DATABASE_URL` (internal URL).
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
- Browser check: run backend with `AUTH_DISABLED=1 DATABASE_URL=...` on :8000 and
  `VITE_API_BASE_URL=http://localhost:8000 npx vite` with empty `VITE_AUTH0_*`, then drive with
  Playwright. This caught a real bug (CORS lacked PUT).
- Sample data for a full campaign: `data/sample_hr_roster.csv`, `data/sample_app_access_aws.csv`,
  `data/sample_app_access_github.csv`. With all three: 11 accounts, 4 queued identities, 12 review
  items (1 critical: terminated Jordan Kim with AWS admin).
- Keep user-facing explanations plain; the user is non-technical about infra and prefers exact
  click-by-click steps for dashboards (Auth0, Render, Vercel, GitHub).
