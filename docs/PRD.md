# UAR Copilot — PRD & Technical Blueprint

Nov 10, 2026 · @Henry

## Executive summary

UAR Copilot should become an agent that runs a full user access review campaign end to end: it pulls access and HR data, detects risk, drafts decisions for each reviewer, chases sign-offs, verifies revocations, and hands the auditor a signed evidence pack. Today the repo is a CSV-in, findings-out rules demo; the core idea (normalize messy exports, apply explainable rules, aggregate by user) is sound and worth keeping as the deterministic engine underneath the agent.

The strategy is a wedge-to-platform path:

1. **Wedge: UAR campaigns (Phases 0–2, about 16 weeks).** Quarterly access reviews are painful, recurring, and directly tested in every SOC 2 audit (CC6.1, CC6.2, CC6.3). Sell to 50–500 person SaaS companies and to MSPs/vCISOs who run reviews for many clients.
2. **Expansion: access-control evidence (Phase 3).** Joiner/mover/leaver checks, privileged access monitoring, and continuous drift detection reuse the same connectors and identity graph.
3. **Platform: SOC 2 control automation (Phase 4+).** Add control tests for change management, vendor risk, and security operations on top of the same evidence ledger, agent runtime and auditor portal.

The design principle that holds it together: **the rules engine decides, the LLM explains and orchestrates, and a human approves.** Every finding must be reproducible without the model, and every action the agent takes must leave an immutable, auditor-readable record.

## Current state: what the repo does today

The repo (16 commits, latest "Add privileged access catalog") is a working prototype: a FastAPI backend on Render and a React/Vite frontend on Vercel. A user uploads two CSVs, the backend merges them on `user_id`, applies two rules, and returns findings grouped by user with a severity summary.

**What to keep**

- **Messy-input normalization** (`rules.py: normalize_columns`, `compute_flags`). Alias plus fuzzy column matching and status value mapping ("Enabled", "TRUE", "1", "Yes") is exactly the pain real exports cause.
- **Declarative rule registry** (`rule_registry.py`). Rules as data with conditions and severity is the right shape; it needs more operators, not a rewrite.
- **Explainable findings.** Each issue carries the exact field values that triggered it. Auditors need this.
- **Privileged access catalog** (`privileged_access_catalog.py`). A tiered catalog of AD, Entra, AWS and Snowflake admin roles. Good seed content.
- **Auth0 JWT validation** (`auth.py`) with JWKS caching.

**Gaps to close before anyone pays for it**

| Area | Finding in the code | Impact |
| --- | --- | --- |
| Security | `require_auth` returns `{}` on missing or invalid token unless `AUTH_STRICT` and `AUTH_REQUIRED` are both set (fail-open) | Anyone can POST access data; unacceptable for a compliance product |
| Security | CORS `allow_origins=["*"]` with `allow_credentials=True`; frontend never sends a bearer token | No real authentication in production |
| Build | Root `requirements.txt` omits `python-jose` and contains a stray line `http://127.0.0.1:8000/docs` | Root install fails; only `backend/requirements.txt` works |
| Rules | Privileged catalog exists but is not wired into `compute_flags`; admin detection is a substring match on "admin" | Misses `db_owner`, `sysadmin`, `AccountAdmin` tiering; no medium findings are ever produced |
| Rules | Only `==` and `!=` operators; `days_since_termination` is computed but unused | No grace periods, dormancy, SoD or time-based rules |
| Identity | Join is exact match on `user_id` across files | Real reviews join HR employee IDs to app emails/UPNs; most matches will be missed |
| Data model | No concept of application, entitlement, reviewer, campaign or decision | Cannot run an actual review, only a scan |
| Persistence | Stateless; nothing stored | No evidence, no history, no audit trail |
| Observability | `print()` for column-mapping warnings | Mapping decisions are invisible to the user and the auditor |
| Quality | No tests, no CI, mixed import paths (`from rules` vs `from backend.rules`) | Fragile deploys |
| Frontend | API URL hard-coded to `uar-copilot.onrender.com`; single page; JSON export only | No reviewer workflow, no evidence export |

Net: roughly 15% of a sellable UAR product exists. The engine logic transfers directly; the product shell (tenancy, workflow, connectors, evidence, agent) is new work.

## Problem and users

User access reviews fail for the same reasons everywhere: data is pulled by hand from a dozen systems, reviewers rubber-stamp spreadsheets they don't understand, revocations are never verified, and the evidence is a folder of screenshots assembled the week before fieldwork. Auditors then sample the review and find terminated users who kept access, which becomes an exception in the SOC 2 report.

The agent's job is to take the GRC owner from "it's review season" to "evidence is in the auditor's folder" with the least human time, while making each human decision better informed.

| Persona | Role in a review | Today's pain | What the agent does for them |
| --- | --- | --- | --- |
| GRC / compliance lead (buyer, primary user) | Scopes and runs the campaign; owns audit evidence | 2–4 weeks of spreadsheet wrangling per quarter; chasing reviewers | Launches a campaign in minutes, sees progress live, exports the evidence pack |
| Reviewer (manager or app owner) | Certifies or revokes each user's access | Long lists with no context; approves everything | Gets a short, pre-triaged queue: "14 low-risk keep, 3 need your decision, here's why" |
| IT / identity admin | Executes revocations | Tickets arrive late and vague | Gets precise remediation tickets; agent verifies the change in the next pull |
| External auditor | Tests the control | Evidence is incomplete, undated, unreproducible | Read-only portal: population, completeness checks, decisions, timestamps, re-performance |
| MSP / vCISO (channel) | Runs reviews for 10–100 clients | Every client is a custom spreadsheet exercise | Multi-tenant console, templates, white-labeled evidence |

Initial ideal customer: B2B SaaS companies of 50–500 employees in their first or second SOC 2 Type II period, running Google Workspace or Entra ID, an HRIS such as BambooHR, Rippling or Gusto, and 10–40 in-scope applications.

## Goals, non-goals and success metrics

**Goals**

- Run a complete, auditor-accepted quarterly UAR for a 200-person company in under 8 hours of total human time.
- Zero terminated-but-active accounts surviving a completed campaign.
- Every finding and decision reproducible from stored inputs without calling an LLM.
- Evidence pack accepted by the customer's auditor without follow-up requests on completeness or timing.

**Non-goals for the first 12 months**

- Autonomous revocation in production systems without human approval. Write-back stays behind explicit approval and per-connector opt-in.
- Replacing an IGA suite (SailPoint, Saviynt). We target companies too small for those.
- Being a full GRC platform in v1. SOC 2 breadth comes after the UAR wedge proves retention.
- Issuing audit opinions. The product produces evidence; the CPA firm tests it.

**Success metrics**

| Metric | Target by GA | How measured |
| --- | --- | --- |
| Campaign cycle time (launch to evidence export) | Under 10 business days, median | Campaign timestamps |
| GRC lead hours per campaign | Under 8 hours | In-app time plus customer survey |
| Reviewer completion without reminders from a human | 85% | Decision timestamps vs reminder log |
| Agent recommendation acceptance rate | 80–95% (below 80 = bad recs; above 95 = possible rubber-stamping) | Decision vs recommendation |
| Identity match rate across HR and apps | 98% auto-matched, remainder resolved in UI | Identity resolution log |
| Finding precision (true issues / flagged) | 90% for high severity | Reviewer "not an issue" feedback |
| Revocations verified in a later pull | 100% of revoke decisions | Remediation status |
| Auditor exceptions related to access reviews | Zero | Customer-reported |

## Functional requirements

Requirements are grouped by the campaign lifecycle. P0 ships in the MVP (Phase 1), P1 by GA (Phase 2), P2 after.

| ID | Requirement | Priority |
| --- | --- | --- |
| **Scope and data intake** |  |  |
| FR-01 | Create a campaign: name, period, in-scope applications, reviewer assignment rule (manager, app owner, or named), due date | P0 |
| FR-02 | Ingest access lists by CSV/XLSX upload with the existing alias and fuzzy column mapping, shown to the user for confirmation before use | P0 |
| FR-03 | Ingest HR roster (employee ID, email, manager, department, status, hire and termination dates) by upload or HRIS connector | P0 |
| FR-04 | Pull access lists via API connectors (Google Workspace, Entra ID, Okta, AWS IAM, GitHub first) | P1 |
| FR-05 | Snapshot and hash every input at ingest; snapshots are immutable and tied to the campaign | P0 |
| FR-06 | Completeness check: compare row counts and totals against the source system and record the result (auditors call this IPE, information produced by the entity) | P1 |
| **Identity resolution** |  |  |
| FR-07 | Match app accounts to HR identities by employee ID, email, UPN, and normalized name, with a confidence score | P0 |
| FR-08 | Queue unmatched and low-confidence accounts for human resolution; classify service, shared and external accounts | P0 |
| **Risk detection (deterministic)** |  |  |
| FR-09 | Rules: terminated but active; active past termination grace period (configurable, default 1 business day); privileged access by catalog tier; orphaned account; dormant account (no login in N days); role mismatch vs department; segregation-of-duties conflicts | P0 (first four), P1 (rest) |
| FR-10 | Rule engine supports `==, !=, >, <, in, not_in, contains, regex, days_since` and per-tenant rule overrides | P0 |
| FR-11 | Each finding stores rule ID and version, input row references and field values, so it can be re-performed | P0 |
| **Agent triage and recommendations** |  |  |
| FR-12 | Agent drafts a recommendation per user-entitlement (keep, revoke, modify, investigate) with a plain-language rationale citing the findings | P0 |
| FR-13 | Agent groups low-risk items for bulk certification but never bulk-approves high-severity items | P0 |
| FR-14 | Agent answers reviewer questions in context ("why does Priya have prod admin?") using only campaign data and connected sources | P1 |
| **Review workflow** |  |  |
| FR-15 | Reviewer queue (web, plus Slack/Teams actions): approve, revoke, modify, delegate, comment; mandatory justification for keeping high-risk access | P0 (web), P1 (Slack/Teams) |
| FR-16 | Agent sends reminders and escalates to the reviewer's manager after the SLA; GRC lead can reassign | P0 |
| FR-17 | Prevent self-review; flag reviewers who approve 100% in under N seconds per item | P1 |
| **Remediation** |  |  |
| FR-18 | Create remediation tickets (Jira, Linear, ServiceNow, email) for revoke and modify decisions | P0 (email/Jira) |
| FR-19 | Verify remediation by re-pulling the source and closing the ticket automatically | P1 |
| FR-20 | Optional connector write-back (disable user, remove group) behind per-action human approval | P2 |
| **Evidence and reporting** |  |  |
| FR-21 | Evidence pack: population snapshots and hashes, completeness results, mapping decisions, findings, decisions with timestamps and actor, remediation proof, campaign sign-off; exported as PDF plus CSV bundle | P0 |
| FR-22 | Auditor portal: read-only, time-boxed access, sampling helper, re-performance of any rule | P1 |
| FR-23 | Trend dashboard across campaigns: findings by app, time to remediate, reviewer behavior | P1 |
| **Administration** |  |  |
| FR-24 | Multi-tenant orgs, SSO (SAML/OIDC), roles: owner, GRC admin, reviewer, auditor, MSP operator | P0 |
| FR-25 | MSP console: manage many client tenants, campaign templates, white-label exports | P1 |

**Non-functional requirements**

- Handle 50,000 account rows per campaign; rule evaluation under 30 seconds for that volume.
- 99.5% availability during business hours at GA; no data loss for submitted decisions (RPO 0 for the decisions table).
- Tenant isolation enforced at the database row level and tested in CI.
- All customer data encrypted in transit (TLS 1.2+) and at rest; per-tenant data export and deletion on request.

## Agent design

The agent is a campaign orchestrator, not a chatbot. It advances a campaign through a fixed state machine, calls typed tools at each stage, and stops at defined human checkpoints. The LLM never decides whether a finding exists; it reads findings the rules engine produced and turns them into recommendations, messages and follow-ups.

&#91;embedded content: campaign loop · 3 lanes, 11 steps\]

Highlighted steps are the only ones the LLM performs; every finding comes from the engine and every decision from a named person.

**Campaign states:** Draft → Ingesting → Resolving identities → Analyzing → In review → Remediating → Verifying → Closed (evidence exported). A scheduler wakes the agent on events (file uploaded, decision submitted, SLA reached, nightly re-pull) rather than running a long-lived loop.

**Tools the agent can call**

| Tool | What it does | Side effect | Human approval |
| --- | --- | --- | --- |
| `ingest_source` | Pull or parse an access list or HR roster, snapshot and hash it | Internal write | No |
| `propose_column_mapping` | Run the existing alias/fuzzy mapper and return mappings with confidence | None | Yes, below 0.85 confidence |
| `resolve_identities` | Match accounts to people; return matched, ambiguous, unmatched | Internal write | Yes, for ambiguous matches |
| `run_rules` | Evaluate the versioned rule set; return findings with input references | Internal write | No |
| `get_context` | Fetch a user's HR record, peer-group access, last login, prior decisions | None | No |
| `draft_recommendation` | Write keep/revoke/modify/investigate with rationale citing finding IDs | Internal write (draft) | Reviewer decides |
| `notify_reviewer` | Send queue link or Slack/Teams card; reminders and escalations | External message | No (templated); GRC lead sets cadence |
| `create_ticket` | Open a remediation ticket for a revoke/modify decision | External write | No (follows a human decision) |
| `verify_remediation` | Re-pull the source and confirm access is gone | Internal write | No |
| `apply_change` (P2) | Disable a user or remove a group in the target system | External change | Yes, per action |
| `export_evidence` | Assemble and sign the evidence pack | Internal write | GRC lead signs off campaign |

**Guardrails**

- **Deterministic core.** Findings, severities and completeness checks come only from code. Recommendations that cite no finding ID, or a non-existent one, are rejected by a schema validator and regenerated.
- **Data minimization.** The model sees only the fields needed for the current item; no bulk dumps of the full population into a prompt.
- **Untrusted data stays data.** Role names, group descriptions and reviewer comments are wrapped and labeled as untrusted input so text inside them cannot steer the agent.
- **Tool allowlist per tenant and per state.** For example, `create_ticket` is unavailable before the review phase.
- **Full trace.** Every model call, tool call, input hash and output is written to the evidence ledger with the model version and prompt template version.
- **Evaluation suite.** A fixed set of synthetic campaigns (built from the existing `data/*.csv` files and expanded) runs in CI; recommendation accuracy, citation validity and refusal-to-bulk-approve-high-risk are tracked per release.

**Model choice.** Use Claude via the API with tool use for orchestration and rationale writing (a larger model for recommendations, a smaller model for message drafting). Keep the model behind an internal interface so it can be swapped or self-hosted for customers who require it.

## System architecture

Keep the current stack (FastAPI, React/Vite, Auth0) and add four things it lacks: a database, a durable workflow engine for weeks-long campaigns, an immutable evidence store, and a connector layer. Stay on Render and Vercel through the design-partner phase; move to AWS before GA so the product can use KMS, private networking and S3 Object Lock for evidence.

&#91;embedded content: system architecture · dashed boxes are external services\]

All services read and write the data layer; only the connectors touch customer systems and only the agent service calls the model.

| Component | Recommendation | Why |
| --- | --- | --- |
| Web app | React + Vite (existing), add React Router and TanStack Query; API base URL from env | Reviewer queue, campaign console, auditor portal in one SPA |
| API | FastAPI (existing), restructured into `app/api`, `app/domain`, `app/rules`, `app/connectors`, `app/agent` | Keeps the current code; fixes the mixed import paths |
| Database | PostgreSQL with row-level security on `tenant_id`; SQLAlchemy 2 + Alembic migrations | Relational data, tenant isolation enforced in the database |
| Workflow engine | Temporal (Cloud) for campaign workflows; start with a Postgres job queue if Temporal is too heavy for Phase 1 | Campaigns run for weeks with timers, retries, reminders and escalations |
| Agent service | Python worker calling Claude with tool use; tools are thin wrappers over domain services | Same code paths as the UI; nothing the agent does is unreachable by a human |
| Rules engine | Existing `rules.py` and `rule_registry.py`, extended operators, rules stored as versioned YAML in the repo plus tenant overrides in the database | Reproducible, reviewable in pull requests |
| Evidence store | S3 with Object Lock (compliance mode) for snapshots and exports; append-only `evidence_event` table with a SHA-256 hash chain | Tamper-evident trail the auditor can verify |
| Connectors | One interface per source: `list_accounts`, `list_entitlements`, `list_people`, `get_last_login`, optional `revoke`; OAuth tokens in AWS Secrets Manager | Add sources without touching the engine |
| Notifications | Email (Postmark or SES), Slack and Teams apps with interactive approve/revoke cards | Reviewers act where they already work |
| Auth | Auth0 Organizations for tenants and SSO; strict mode always on; roles in the token | Reuses existing integration; removes fail-open path |
| Observability | OpenTelemetry traces, Sentry, structured JSON logs; LLM call traces to the evidence ledger | Debugging and auditability |

## Data model

The current `Finding` and `Issue` schemas become one layer of a larger model. Every table carries `tenant_id`, `created_at` and `created_by`; decisions and evidence events are append-only.

| Entity | Key fields | Notes |
| --- | --- | --- |
| `tenant` | id, name, parent\_tenant\_id, plan | `parent_tenant_id` lets an MSP own client tenants |
| `person` | id, employee\_id, email, name, manager\_id, department, title, employment\_status, hire\_date, termination\_date | From the HR roster; the identity anchor |
| `application` | id, name, owner\_person\_id, criticality, in\_scope\_soc2, connector\_id | Owner is the default reviewer |
| `account` | id, application\_id, native\_id, username, email, status, last\_login\_at, account\_type (human, service, shared, external) | One per user per app |
| `identity_link` | account\_id, person\_id, method, confidence, confirmed\_by | Output of identity resolution |
| `entitlement` | id, application\_id, name, description, privilege\_tier, category | Tier and category from the privileged access catalog |
| `account_entitlement` | account\_id, entitlement\_id, granted\_at | The access being reviewed |
| `source_snapshot` | id, campaign\_id, source, file\_uri, sha256, row\_count, completeness\_status | Immutable; points to S3 object |
| `campaign` | id, name, period\_start, period\_end, state, due\_date, rule\_set\_version, signed\_off\_by, signed\_off\_at | Lifecycle state for the workflow |
| `review_item` | id, campaign\_id, account\_entitlement\_id, reviewer\_id, recommendation, rationale, finding\_ids, status | One per account-entitlement in scope |
| `finding` | id, campaign\_id, rule\_id, rule\_version, severity, account\_id, inputs (json), explanation | Extends today's `Issue` with references |
| `decision` | id, review\_item\_id, actor\_id, action, justification, decided\_at, channel | Append-only; a change creates a new row |
| `remediation` | id, decision\_id, ticket\_ref, status, verified\_snapshot\_id, verified\_at | Closed only by a later snapshot |
| `evidence_event` | id, campaign\_id, type, payload, actor (human or agent), prev\_hash, hash | Hash chain over every event |
| `rule` | id, version, definition (yaml), severity, control\_refs | Control refs map rules to SOC 2 criteria |

A review item is the unit of work: one person's one entitlement in one application during one campaign. That granularity is what auditors sample.

## Integrations and connectors

File upload stays a first-class connector forever: every customer has at least one system with no usable API. API connectors are prioritized by how often they appear in a 50–500 person SaaS company's SOC 2 scope.

| Wave | Connectors | Data pulled | Phase |
| --- | --- | --- | --- |
| 0 | CSV / XLSX upload (existing mapper) | Accounts, roles, HR roster | 1 |
| 1 | Google Workspace, Microsoft Entra ID, Okta | Users, groups, admin roles, last sign-in | 1–2 |
| 1 | BambooHR, Rippling, Gusto, HiBob | People, managers, status, termination dates | 1–2 |
| 2 | AWS IAM Identity Center + IAM, GCP IAM, GitHub, Snowflake | Principals, policies, org/admin roles | 2 |
| 2 | Jira, Linear, Slack, Microsoft Teams | Tickets out; reviewer actions in | 2 |
| 3 | Salesforce, HubSpot, Datadog, PagerDuty, Postgres/MySQL role exports, Azure subscriptions | Accounts and roles | 3 |
| 3 | Vanta, Drata, Secureframe (push evidence) | Evidence pack upload via their APIs | 3 |

**Connector contract.** Each connector implements read methods returning normalized records plus the raw payload (stored in the snapshot), declares which fields it can supply (for example, last login), and is tested against recorded fixtures. Read-only scopes by default; write scopes are a separate, explicit grant.

**Channel note.** Vanta and Drata already ship access review modules, so they are competitors as well as integration targets. Pushing evidence into them lets a customer keep their GRC platform while using UAR Copilot for the deeper work those modules don't do: identity resolution across HR and apps, agent triage, reviewer chasing and verified remediation.

## Security and trust posture

Customers will hand this product a map of who can access what across their company, which makes it a high-value target. The product must pass the same review it helps customers pass: plan for UAR Copilot's own SOC 2 Type I within about 6 months of the first paying customer and Type II 6–12 months after that.

**Fix immediately (Phase 0)**

- Make authentication fail closed: delete the permissive fallbacks in `require_auth`, require a valid token on every route except `/health`.
- Restrict CORS to the deployed frontend origins; send the Auth0 access token from the frontend.
- Fix `requirements.txt` (add `python-jose[cryptography]`, remove the stray URL line) and pin versions.
- Replace `print()` with structured logging; never log row contents.

**Platform controls (Phases 1–2)**

- Tenant isolation via Postgres row-level security, with automated cross-tenant access tests in CI.
- Encryption at rest with KMS; per-tenant data keys for snapshots and evidence.
- Connector credentials only in a secrets manager; least-privilege, read-only OAuth scopes by default.
- Data retention policy per tenant (default: snapshots kept for the audit period plus 1 year), with verified deletion.
- Admin access to production through SSO, MFA and just-in-time elevation, reviewed quarterly using the product itself.
- LLM data handling: use a provider configuration with zero data retention where available; disclose subprocessors; offer a mode that sends no PII to the model (pseudonymized identifiers, rationale templated server-side).
- Dependency scanning, SAST and secret scanning in CI; annual third-party penetration test before GA.

## SOC 2 expansion path

The UAR build produces three assets that every other SOC 2 control needs: an identity graph (people, accounts, entitlements), a connector layer into the customer's systems, and an evidence ledger auditors trust. Each expansion module is a new set of rules, agent tools and evidence templates on that same base, so the marginal cost per control falls with each module.

| Module | SOC 2 criteria (2017 TSC) | What the agent does | Reuses | Phase |
| --- | --- | --- | --- | --- |
| Access reviews (wedge) | CC6.1, CC6.2, CC6.3 | Runs quarterly campaigns, triages, chases, verifies revocation | Base | 1–2 |
| Joiner / mover / leaver monitoring | CC6.2, CC6.3, CC6.5 | Watches HR events daily; flags access not removed within SLA after termination or role change; opens tickets | Identity graph, connectors | 3 |
| Privileged access monitoring | CC6.1, CC6.3, CC7.2 | Continuous drift detection on admin roles; requires justification for new grants | Privileged catalog, rules | 3 |
| MFA and authentication config | CC6.1, CC6.6 | Tests MFA enforcement, SSO coverage, password policy per app | Connectors | 3 |
| Personnel controls | CC1.4, CC1.5, CC2.2 | Tracks background checks, security training, policy acknowledgements per person | Person table, HRIS | 4 |
| Change management | CC8.1 | Tests pull requests for review and approval before merge to production; flags direct pushes | GitHub connector | 4 |
| Vendor risk | CC9.2 | Inventories vendors from SSO and spend data; tracks SOC reports and reviews | Connectors, evidence ledger | 4 |
| Vulnerability and config monitoring | CC7.1 | Pulls scanner and cloud posture results; tracks remediation SLAs | Rules, tickets | 4 |
| Incident response | CC7.3, CC7.4, CC7.5 | Tracks incidents, postmortems and tabletop exercises as evidence | Evidence ledger | 5 |
| Risk assessment and policies | CC3.1–CC3.4, CC5.3 | Drafts the annual risk assessment and policy reviews from system data for human editing | Agent, evidence ledger | 5 |

**How the product changes as it expands**

- **From campaigns to continuous monitoring.** UAR is periodic; JML and privileged monitoring run daily. The workflow engine already supports both.
- **From findings to control tests.** Each module maps rules to control IDs (the `control_refs` field on `rule`), so the auditor portal can show pass/fail by criterion with linked evidence.
- **From one buyer to two.** GRC leads buy UAR; at module 3+ the buyer becomes whoever owns the SOC 2 program, often the CTO at this company size. Pricing moves from per-campaign or per-identity to a platform tier.
- **Competitive position.** Compliance automation platforms (Vanta, Drata, Secureframe) are broad and checklist-driven. The position here is the agent that does the work behind the checklist, starting with the most labor-intensive control, and the MSP channel those platforms serve less directly.

## Delivery roadmap

Five phases, each ending at a gate that must be met before the next starts. Durations assume two full-time engineers plus a part-time designer; a solo builder should roughly double them.

&#91;embedded content: delivery roadmap · 5 phases, 4 gates\]

The gate after Phase 2 is the one that matters most: an auditor accepting the evidence pack is the proof the product works.

**Phase 0 — Harden the prototype (weeks 1–2)**

- [ ] Make `require_auth` fail closed; send Auth0 token from `App.tsx`; restrict CORS
- [ ] Fix root `requirements.txt`; pin versions; add `pyproject.toml`
- [ ] Restructure backend into `app/` package; one import style
- [ ] Wire `classify_privileged_access` into `compute_flags`; emit tiered severities (critical, high, medium)
- [ ] Add grace-period rule using `days_since_termination`; add operators to `evaluate_condition`
- [ ] Return column-mapping decisions and confidence in the API response; show them in the UI
- [ ] Pytest suite over `data/*.csv`; GitHub Actions CI; API URL from env in the frontend

**Phase 1 — MVP campaign (weeks 3–10)**

- [ ] Postgres schema and migrations for the data model; row-level security
- [ ] Campaign workflow (create, ingest, resolve, analyze, review, close) on a job queue
- [ ] Identity resolution with confidence and a manual-match screen
- [ ] Agent service: `draft_recommendation`, `notify_reviewer`, reminders and escalation
- [ ] Reviewer queue UI with bulk-certify for low risk and mandatory justification for high risk
- [ ] Evidence ledger with hash chain; PDF + CSV evidence export
- [ ] Google Workspace and one HRIS connector; email and Jira remediation tickets
- [ ] Recruit 3 design partners (at least one MSP)

**Phase 2 — GA (weeks 11–18)**

- [ ] Entra ID, Okta, AWS, GitHub connectors; completeness checks
- [ ] Slack and Teams reviewer actions; reviewer-behavior flags
- [ ] Remediation verification by re-pull
- [ ] Auditor portal with re-performance; MSP multi-tenant console
- [ ] Agent eval suite in CI; move hosting to AWS; penetration test

**Phase 3 — Continuous access (months 5–9):** JML monitoring, privileged access monitoring, MFA config tests, Vanta/Drata evidence push, UAR Copilot's own SOC 2 Type I.

**Phase 4 — SOC 2 platform (months 9–15):** personnel, change management, vendor risk and vulnerability modules; control-level dashboard for the auditor; own SOC 2 Type II.

## Risks and open questions

| Risk | Likelihood | Mitigation |
| --- | --- | --- |
| Auditors distrust AI-generated recommendations | Medium | Findings are deterministic and re-performable; the agent only drafts; every decision is a named human's; ship a one-page "how the agent works" memo for auditors |
| Reviewers rubber-stamp the agent's recommendations | High | Track acceptance rate and time per item; never bulk-approve high risk; require justification; surface outliers to the GRC lead |
| Identity matching errors cause wrong revocations | Medium | Confidence thresholds, human confirmation for ambiguous links, no write-back without per-action approval |
| Compliance platforms bundle "good enough" access reviews | High | Differentiate on depth (identity resolution, chasing, verified remediation) and integrate with them rather than compete head-on; lead with the MSP channel |
| Connector maintenance consumes the team | High | Upload as universal fallback; connector contract with recorded fixtures; prioritize by design-partner demand |
| A breach of the product exposes customers' access maps | Low, severe | Phase 0 fixes first; tenant isolation tests; own SOC 2; minimal data retention |
| LLM cost per campaign erodes margin | Low | Only high and medium items get model-written rationale; low-risk items use templates; cache context |

**Open questions**

- [ ] Lead market: direct to SaaS companies, or MSPs/vCISOs first? This changes Phase 1 priorities (MSP console moves earlier).
- [ ] Pricing unit: per identity per year, per campaign, or per client tenant for MSPs?
- [ ] Which HRIS and IdP do the first three design partners use? Build those connectors first.
- [ ] Will a partner CPA firm review the evidence pack format before Phase 2 GA?
- [ ] Is customer-controlled model hosting a hard requirement for any target segment?
