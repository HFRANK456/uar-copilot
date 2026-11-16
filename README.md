# UAR Copilot

User access review copilot. Deterministic rules engine (FastAPI) plus a React/Vite UI.
See the PRD (`docs/PRD.md`) for the roadmap; this repo implements Phase 0 and the Phase 1 campaign flow.

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
| `ANTHROPIC_API_KEY` | For Claude suggestions | Claude API key used to draft reviewer suggestions. Without it, **Draft suggestions with Claude** returns 503 and everything else works |
| `CONNECTOR_SECRET_KEY` | For connections | Fernet key that encrypts connection credentials at rest. Generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. Without it, adding a connection returns 503. Changing it makes saved connections unreadable (re-add them) |

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
| `external_account_active` (campaigns) | medium |
| `dormant_account` (no login in 90+ days, campaigns) | low |
| `admin_role` (looks admin, not in catalog) | low |

Every issue carries `rule_version`, the `inputs` that triggered it, a `row_ref` and SOC 2 `control_refs`,
and the response includes column-mapping decisions and a SHA-256 of each uploaded file.

Identity matching links accounts to HR people by employee ID (1.00), email (0.99), username vs email
local part (0.92), display name (0.80) or username pattern such as `pshah` (0.75). Links at 0.90 or above
are automatic; weaker or tied matches queue for a person, service/shared accounts are set aside, and
accounts on an unknown email domain are marked external. Re-running never overwrites a manual decision.

The evidence pack contains `report.pdf`, `manifest.json` (SHA-256 of every file plus chain verification),
the uploaded files byte-for-byte under `population/`, CSVs for column mappings, identities, findings,
review items and full decision history, `evidence_events.jsonl` (the hash chain) and a `README.txt`
explaining how to verify it independently.

Who can decide: the item's assigned reviewer, or the campaign owner (its creator) with an `override_reason`
that is stored on the decision and shown in the evidence pack. Only the owner can reassign, set app owners,
sign off, manage remediation and close.

## Workspaces

Everyone gets a personal workspace on first sign-in. Owners invite people by email with a role
(owner, admin, reviewer, auditor); the invitee signs in with that email and clicks **Join**. One person
can belong to several workspaces (e.g. an MSP with one per client); the browser sends `X-Workspace-Id`.

| Endpoint | Purpose |
| --- | --- |
| `GET /workspaces` | Your workspaces and pending invitations |
| `POST /workspaces` | Create a workspace (you become owner) |
| `PATCH /workspaces/{id}` | Rename (owner) |
| `GET/POST /workspaces/{id}/members` | List / invite (owner) |
| `PATCH/DELETE /workspaces/{id}/members/{member}` | Change role / remove (owner; a workspace keeps at least one owner) |
| `POST /workspaces/{id}/invitation/accept` or `/decline` | Answer an invitation |

## Connections (Google Workspace, Jira Cloud)

Workspace owners and admins connect systems under **Workspace → Connections**; a campaign can then
**Pull from a connection** instead of uploading a file. Credentials are encrypted with
`CONNECTOR_SECRET_KEY` and never returned by the API. Code: `backend/connectors/` (one module per
system with `validate`, `test`, `pull`), `backend/domain/connections.py`, `backend/api/connections.py`.

| System | Credentials | Pulls | Writes |
| --- | --- | --- | --- |
| Google Workspace | Service account JSON key with domain-wide delegation for the read-only scopes `admin.directory.user.readonly` and `admin.directory.rolemanagement.readonly`, plus an admin email to act as | Every user (suspended/archived → inactive, last sign-in) and each admin role as an entitlement | Nothing |
| Jira Cloud | Site, Atlassian account email, API token; optional project key | Atlassian and app (bot) accounts with their groups as entitlements; portal customers skipped. No last-login in Jira's API, and hidden emails fall back to name matching | Remediation tickets in the chosen project (**Send to Jira**), only when clicked |

A pull is stored like an upload: the raw API responses (JSON) are the snapshot, hashed and included
in the evidence pack, and the ledger gets `source.ingested` plus `source.pulled`. Tickets sent to
Jira set each remediation's ticket number and record `remediation.ticket_sent`.

| Endpoint | Purpose |
| --- | --- |
| `GET/POST /connections` | List / add (owner, admin) |
| `POST /connections/{id}/test` | Check the credentials; saves status `ok` or `error` |
| `DELETE /connections/{id}` | Remove (data already pulled stays in campaigns) |
| `POST /campaigns/{id}/sources/connection` | `{connection_id, application?}`: pull an access list |
| `POST /campaigns/{id}/remediations/send-tickets` | `{connection_id}`: a Jira ticket per open change without one |
| `POST /campaigns/{id}/remediations/verify-pull` | `{application_id, connection_id}`: confirm changes by pulling that application again from the connection that supplied it (same check as a re-upload; `verification_method` `repull`, event `remediation.verification_pull`) |

In the review list, service, shared and bot accounts (such as Atlassian's built-in app accounts)
are listed after people, and an **Accounts** filter shows people only or bots only (hidden items are
counted, with how many have findings).

Tests run the connectors against recorded-format responses through `httpx.MockTransport`
(`backend.connectors.base.transport_factory`); nothing calls Google or Atlassian.

## Claude suggestions for reviewers

After analysis (and until sign-off), a workspace owner or admin can click **Draft suggestions with
Claude**. For every item with findings, Claude (`claude-opus-5-5`, with Anthropic's default refusal
fallback) drafts keep / revoke / modify, a short rationale and the findings it relied on. The rules
engine still decides what is risky; Claude only explains, and a person makes every decision.

- **What is sent**: application, entitlement, privilege tier, account type/status, days since last
  login, the person's department, title, employment status and dates, and each finding's rule,
  severity, description and non-identifying inputs. Never names, emails, usernames or account IDs;
  items are labelled `I1`, findings `I1-F1`.
- **Validation**: structured output against a JSON schema, then every suggestion must answer an item
  that was sent and cite at least one of that item's own findings, or it is dropped.
- **Evidence**: `recommendation.generated` events record the model requested and served, prompt and
  rule-set versions, the SHA-256 of exactly what was sent, and every suggestion. `review_items.csv` in
  the pack has `ai_suggestion`, `ai_confidence`, `ai_rationale`, `ai_model` and
  `ai_suggestion_followed`.
- Code: `backend/llm.py` (client; tests swap `client_factory` for a fake), `backend/domain/recommendations.py`,
  `POST /campaigns/{id}/recommendations`.
