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
| 8 | #12 | Connectors: `connection` table (migration 0006, RLS), credentials Fernet-encrypted with env `CONNECTOR_SECRET_KEY`, Google Workspace (service account + domain-wide delegation, read-only: users, suspended, last login, admin roles) and Jira Cloud (email + API token: users/app accounts with groups; creates remediation tickets). Workspace → Connections (owner/admin; add, test, remove); campaign Sources "Pull from a connection" (raw API JSON is the snapshot, `source.pulled` event); Remediation "Send to Jira". Catalog knows Google/Jira admin roles (rule set 2026.11.3). **Jira verified live 2026-11-15** on the user's free test site `uarcopilot-test.atlassian.net` (project key `KAN`): pull of 44 accounts / 30 group grants (most are Atlassian built-in bot accounts), and Send to Jira created KAN-4. Google not yet tested live |
| 9 | #13, #14 | Review list: people first, Accounts filter (people / service and bot accounts) with a hidden-items count; remediation verify by re-pulling from the connection that supplied the application (`POST …/remediations/verify-pull`, method `repull`); remediation shows usernames instead of opaque IDs (Jira); verify result stays visible after the last change is confirmed (#14); `/live-check set up Jira` (#13) |
| 10 | #15 | Claude suggestions for reviewers (PRD C): `POST /campaigns/{id}/recommendations` (managers; analyzing or in_review) sends de-identified flagged items (no names/emails/usernames/IDs) to `claude-opus-5-5` with `fallbacks: "default"` and a JSON schema; each suggestion must cite its own item's findings or is dropped; stored on `review_item.recommendation/rationale/recommendation_meta` (migration 0007); `recommendation.generated` ledger event with payload SHA-256; pack `review_items.csv` gains `ai_*` columns incl. `ai_suggestion_followed`. Needs `ANTHROPIC_API_KEY` on Render. Not yet run against the real API (no key in the sandbox) |
| 11 | #16 | Microsoft Entra ID connector (`connectors/entra_id.py`, kind `entra_id`): app registration with client credentials (tenant ID, client ID, secret), Graph application permissions User.Read.All, RoleManagement.Read.Directory, AuditLog.Read.All; users (disabled, guests, `signInActivity` with fallback when the tenant lacks P1/P2) and `directoryRoles?$expand=members` as entitlements; catalog adds Entra roles (rule set 2026.11.4); Connections form with setup steps; `/live-check set up Entra`. Tested with recorded-format Graph responses only; not yet against a real tenant |
| 12 | #17 | Email via Resend (`backend/mailer.py`, best-effort, `mailer.transport_factory` for tests): workspace invitation emails (re-inviting an invited person updates the role and resends); on start review each assigned reviewer is emailed and auto-added as an invited Reviewer member (`domain/notifications.py`, inside a savepoint so email never blocks the review); owner "Email reminders" button (`POST /campaigns/{id}/reminders`) copies the reviewer's HR manager once the due date has passed; ledger events `review.reviewers_notified`, `review.reminders_sent`; emails carry counts + link only. Env `RESEND_API_KEY`, `EMAIL_FROM`, `APP_URL`. No automatic schedule yet (reminders are a button) |
| 13 | #18 | Weekly automatic reminders: GitHub Actions `.github/workflows/weekly-reminders.yml` (Mondays 14:17 UTC + manual run) POSTs `/internal/weekly-reminders` with `X-Cron-Secret` (env `CRON_SECRET` on Render = repo Actions secret `CRON_SECRET`; skips quietly if the GitHub secret is unset); `notifications.run_weekly` reminds in-review campaigns with `campaign.auto_reminders` (migration 0008, default on, owner toggle `POST /campaigns/{id}/auto-reminders`) not emailed in 6 days (`campaign.last_reminder_at`, set by review start and every reminder); ledger actor `system:weekly-reminders`. Chosen over an in-app timer because the Render web service is on the **free plan** and sleeps |

**Phase 1 remaining, in the recommended order:**
- B2. Tickets by email are built: campaign managers can email one ticket per unticketed open/done
  remediation to an IT/helpdesk address through Resend (`POST …/remediations/email-tickets`).
  Emails include the account, entitlement, risk, requested change, remediation ID and campaign link;
  the form explains this before sending. Successful sends set `ticket_ref` to `email:<message-id>`
  and record `remediation.ticket_emailed` (recipient and reference). Failure records
  `remediation.ticket_email_failed`, stops the batch, and preserves earlier successes; retry skips
  existing tickets (including Jira) and resolved changes. Delivery does not mark a change done or
  verified. Uses the existing Resend setup; no new environment variables or migration.
- **Connectors, next ones** (decided 2026-11-15): Google Workspace and Jira Cloud are built (#12).
  Entra ID is built (#16). Next: HR rosters through Finch (decided 2026-11-16: MSP clients are mostly
  on payroll providers like Gusto, ADP, Paychex, Justworks, Rippling; Finch covers them with one
  integration), then Okta, GitHub, AWS IAM Identity Center. Workday
  directly only when a larger customer needs it. Also: expand Google admin roles assigned to groups
  (today they produce a warning).
- Verify a sending domain in Resend and set `EMAIL_FROM` (until then Resend only delivers to the account owner).
- Also from the PRD Phase 0 list, still open: restructure backend into an `app/` package (deferred;
  current Render start command `uvicorn main:app` from `backend/` must keep working).

## Architecture
- **Backend**: FastAPI in `backend/`. `main.py` (app, CORS, JSON logging, migrations on startup),
  `auth.py` (Auth0 JWT, fail closed; fetches email from `/userinfo` when the token lacks it),
  `rules.py` + `rule_registry.py` (versioned rules, `RULE_SET_VERSION`), `mapping.py` (column
  mapper), `privileged_access_catalog.py`, `db/models.py`, `db/session.py` (`tenant_session` sets
  `app.tenant_id` with SET LOCAL), `domain/` (`campaigns`, `evidence`, `ingest`, `identity`,
  `analysis`, `review` (authority, decisions, sign-off), `remediation`, `export` (evidence pack),
  `workspaces`, `connections`, `recommendations`, `notifications`), `llm.py` (Claude client), `mailer.py` (Resend), `connectors/` (`base` httpx client + errors, `secrets` Fernet,
  `google_workspace`, `jira_cloud`, `entra_id`), `api/campaigns.py` (all `/campaigns` routes),
  `api/workspaces.py` (`/workspaces`), `api/connections.py` (`/connections`), `api/internal.py`
  (`/internal/*`, scheduled jobs, `X-Cron-Secret` instead of Auth0).
  Schemas in `schemas/user_schema.py` (repo root). Imports always `backend.*`.
- **Migrations**: `backend/migrations/versions/0001..0008`. A new tenant table must enable and force
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
- **Rules (2026.11.4)**: terminated_privileged_active (critical), terminated_active past grace
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
  it without re-adding connections), `ANTHROPIC_API_KEY` (Claude suggestions; added by the user
  2026-11-16), `RESEND_API_KEY` / `EMAIL_FROM` (email; the user adds them), `CRON_SECRET`
  (weekly reminders; the user sets the same value here and as the GitHub Actions secret). The web
  service is on Render's **free plan** (sleeps when idle; first request after sleep is slow).
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
