---
name: live-check
description: Check the live UAR Copilot site (https://uar-copilot.vercel.app) in the user's Chrome after a deploy, either the standard end-to-end campaign check or one specific action the user names. Use when the user says "live check", "/live-check", "test it on the live site", or a development session asks for a live verification. Needs Claude in Chrome on the user's computer.
---

# Live check of UAR Copilot

You are checking the deployed app in the user's own Chrome, through Claude in Chrome. The goal is
to confirm a change works on the real site with real sign-in, and to report exactly what happened.

## Before you start
- You need the Claude in Chrome tools. If they are not available, stop and tell the user to run this
  from the Claude desktop app with the Claude in Chrome extension installed and connected.
- Work in a **new tab**. Don't touch the user's other tabs.
- **Never type a password or complete a sign-in yourself.** If the Auth0 sign-in page appears, ask
  the user to sign in in that tab, wait for them to say they're done, then continue.
- Don't delete anything, and don't change any campaign other than the one this check is about.
- Sample files live in this repo's `data/` folder: `sample_hr_roster.csv`,
  `sample_app_access_aws.csv`, `sample_app_access_github.csv`, and for remediation
  `sample_app_access_aws_after_remediation.csv`. If you can't attach files to a
  file input yourself, ask the user to choose the named file in the open dialog, then continue.

## What to run
- **With arguments**: do exactly the action the user named (for example "sign off Test Review and
  download the evidence pack"), then run the checks for that part from the standard flow below.
- **Without arguments**: run the standard flow on a **new** campaign named
  `Live check <YYYY-MM-DD HH:MM>`, so existing campaigns are left alone.

## Standard flow and what each step should show
1. Open https://uar-copilot.vercel.app. It should land on **Campaigns** (the URL ends in `/campaigns`),
   with the current workspace name shown top right.
   If a red "Sign-in failed" banner shows, copy its full text into the report and stop.
2. Sign in if needed (the user does this). The user's email should appear top right.
3. Create the campaign. It opens on its own page in state **Draft**.
4. Upload `sample_hr_roster.csv` in **HR roster** → message "Loaded 6 rows".
   Upload `sample_app_access_aws.csv` with application `AWS`, then
   `sample_app_access_github.csv` with application `GitHub` → three rows under Sources.
5. **Match accounts to people** → "4 accounts still need a decision". Resolve them: the suggested
   "This is …" button for `pshah`, `mlee`, `dreyes`; **External user** for `ghost-user`.
   → "Every account is resolved."
6. **Run analysis** → 12 review items: 1 critical (Jordan Kim, AWS AdministratorAccess,
   terminated), 1 high (Priya Shah), 2 medium, 1 low, 7 with no findings.
6b. Under **Risk analysis**, a **Suggestions from Claude** box says it sends "never names or
   emails". Only if the user asked (it uses their Claude credit, a few cents), click **Draft
   suggestions with Claude** → "Claude drafted 5 suggestion(s) for 5 flagged item(s)", and each
   flagged item shows a "CLAUDE SUGGESTS …" box with a reason and "Based on: …". Jordan Kim's
   AdministratorAccess should be **Revoke**; report the others as they are (wording varies). If it
   says ANTHROPIC_API_KEY is not set, report that and carry on.
7. **Start review** → a notice "Review started. Emailed N reviewer(s) their items…" (or, if email
   isn't set up, "Email isn't set up yet…"). Report which one appeared. Don't click **Email
   reminders** unless the user asks (it emails real people from the roster if their addresses exist).
   Then continue: You're the campaign owner, not the assigned reviewer, so each item says
   "To decide it yourself, add an override reason". Check that, then type
   "Live check: owner deciding for reviewers" in the **Deciding for reviewers?** box.
   On the critical item click **Keep**: a justification box must appear and its button must stay
   disabled under 10 characters. Cancel, then **Revoke**; the decision must say
   "(campaign owner)" and show the override reason. On the high item, Keep with the justification
   "Live check: on-call admin approved". Under **All**, use **Keep N low-risk items shown**.
   Revoke the AWS medium item (contractor.ext); **Modify…** the GitHub medium item (ghost-user)
   with "Downgrade to read access only". → "12 of 12 decided".
8. **Sign off campaign** with the attestation "Live check". → state badge **Remediating**, the
   Keep/Revoke buttons disappear, and a **Remediation** section shows "0 of 3 confirmed".
9. **Remediation**: **Download tickets (CSV for Jira import)** downloads a CSV. The owner also sees
   **Email tickets to IT**, with an address field and a note explaining which access details are sent.
   Only when the user explicitly requests email delivery, enter their chosen IT/helpdesk address and
   click **Email tickets**. Expect "Emailed N ticket(s)." and `email:…` references on sent changes;
   changes stay open until completed and verified. Existing tickets and resolved changes are skipped.
   If sending fails, successful sends stay saved and retry sends only the remainder. Check the
   evidence trail for `remediation.ticket_emailed` (recipient and reference), or
   `remediation.ticket_email_failed`. Without Resend set up, expect a clear RESEND_API_KEY message.
   On jordan.kim, enter
   ticket `IT-101`, **Save ticket**, **Mark done** → "Done, not verified". In **Verify with a fresh
   access list**, choose **AWS** and `data/sample_app_access_aws_after_remediation.csv`, then
   **Check changes** → "AWS: 1 of 2 change(s) confirmed" and "Still there: contractor.ext …";
   jordan.kim shows **Verified**. **Close campaign** must still be disabled. Then **Accept without
   upload…** on contractor.ext and ghost-user with the reason "Live check: removal confirmed by the
   vendor by email" → "3 of 3 confirmed". **Close campaign** → badge **Closed**.
10. **Download evidence pack** → a file `evidence-Live_check_….zip` downloads. **Show events**:
   the newest event is `evidence.exported`, and the header says "Hash chain verified".
11. Open **Quick scan** in the nav: the page must say "Nothing is saved".
12. Click **Workspace** (top right): the page lists the user as **owner**, with an invite form and a
    **New workspace** form. Don't invite anyone or create workspaces unless the user asks.
13. On the same page, a **Connections** section shows "Add a connection" with Google Workspace and
    Jira Cloud. Choosing one shows its setup steps and fields. **Don't add a connection** and never
    type keys or tokens yourself: the user does that. If connections already exist, each shows
    **Working**, **Not working** (with the reason) or **Not tested**; clicking **Test** is fine.
    In a campaign still taking sources, the Sources section has a **Pull from a connection** box.

## Checking a connection (only when the user asks, e.g. `/live-check pull from Jira`)
The user adds the connection in **Workspace → Connections** themselves (they paste the token or
choose the key file). Then, on a new campaign with `sample_hr_roster.csv` uploaded, **Pull access
list** from that connection → "Pulled N accounts with M entitlement grants from …", and a new row
under Sources says "via Google Workspace" or "via Jira Cloud". Report the counts and any warnings
shown; they come from the user's real systems, so there are no fixed expected numbers. Don't run
**Send to Jira** unless the user asks: it creates real tickets.

## Setting up a free test Jira (only when the user asks, e.g. `/live-check set up Jira`)
Goal: a free Jira Cloud site connected to UAR Copilot, proven with a real pull and real tickets.
The user does every step that involves their identity or a secret; you do the clicking around it.
1. Ask whether they already have an Atlassian account. If not, open
   https://www.atlassian.com/software/jira/free and **hand over**: the user enters their email,
   password and the verification code themselves. Wait until they say they're signed in.
2. Help them finish Jira's first-run screens: site name (suggest `uarcopilot-test`, or what they
   like), skip inviting teammates, create a project from any template (suggest key `IT`).
   Note the site address (`….atlassian.net`) and the project key.
3. Open https://id.atlassian.com/manage-profile/security/api-tokens. Ask the user to click
   **Create API token**, name it "UAR Copilot", and **copy it themselves**. Never read the token
   from the screen, copy it, or type it anywhere.
4. Open https://uar-copilot.vercel.app/workspace → **Connections** → **Add a connection** →
   **Jira Cloud**. Fill in the site, the user's Atlassian email and the project key. Then ask the
   user to click into **API token** and paste it. Click **Connect and test** → expect
   "Jira Cloud connected. Signed in to https://….atlassian.net as <their name>." and **Working**.
5. Prove the pull: new campaign `Jira check <YYYY-MM-DD HH:MM>`, upload `sample_hr_roster.csv`,
   **Pull from a connection** → Jira Cloud → "Pulled N accounts … from Jira Cloud" (a fresh free
   site has the user plus a few Atlassian app accounts; report N and the warnings).
6. Prove tickets (creates real tickets in the test site only, so confirm with the user first): on
   a campaign that is in **Remediating** with open changes (the standard flow's campaign after
   step 8 works), click **Send to Jira (IT)** → "Created N Jira ticket(s): IT-1 …". Open the
   Jira project and confirm the tickets are there with the label `access-review`.
If anything fails, report the exact message the Connections list shows under **Not working**.

## Setting up Microsoft Entra ID (only when the user asks, e.g. `/live-check set up Entra`)
The user needs admin rights in a Microsoft 365 / Entra tenant (their own, or a free one from an
Azure account). They do every sign-in and handle the secret; you do the clicking around it.
1. Open https://entra.microsoft.com and **hand over** for sign-in. Then: Applications → App
   registrations → New registration → name "UAR Copilot", Single tenant → Register.
2. From the app's Overview, note the **Application (client) ID** and **Directory (tenant) ID**
   (these are not secret).
3. API permissions → Add a permission → Microsoft Graph → Application permissions → tick
   `User.Read.All`, `RoleManagement.Read.Directory`, `AuditLog.Read.All` → Add → **Grant admin
   consent** (the user confirms the consent prompt).
4. Certificates & secrets → New client secret → Add. Ask the user to **copy the Value
   themselves**. Never read the secret from the screen, copy it, or type it.
5. In https://uar-copilot.vercel.app/workspace → Connections → Add a connection → Microsoft Entra
   ID: fill in the tenant ID and client ID, ask the user to paste the secret, **Connect and test**
   → "Microsoft Entra ID connected. Signed in to tenant …" and **Working**.
6. New campaign `Entra check <YYYY-MM-DD HH:MM>`, upload `sample_hr_roster.csv`, **Pull from a
   connection** → Microsoft Entra ID → report the counts and warnings exactly (a tenant without
   Entra ID P1/P2 shows "Last sign-in dates aren't available"; that's expected).

## Setting up weekly reminders (only when the user asks, e.g. `/live-check set up weekly reminders`)
Goal: the Monday reminder job works end to end. It needs the same secret (`CRON_SECRET`) on the
Render backend and in GitHub, and email (`RESEND_API_KEY`) on Render. **The user creates and pastes
every secret value**; you never generate, read, copy or type one, and never put one in your report.
1. Ask the user to create a long random value (30+ characters, e.g. their password manager's
   generator) and keep it on their clipboard. Wait until they say it's ready.
2. **Render**: open https://dashboard.render.com/web/srv-d7a5jrbuibrs73fqkiu0/env (the
   **uar-copilot** web service, not the database). If asked to sign in, hand over. Click **Add
   Environment Variable**, type the key `CRON_SECRET`, then ask the user to paste the value into the
   value box. If `RESEND_API_KEY` isn't in the list yet, say so and ask whether to add it now: the
   user signs up at resend.com, creates an API key and pastes it; you only type the key name. Click
   **Save, rebuild, and deploy**. Don't change or delete any other variable.
3. **GitHub**: open https://github.com/HFRANK456/uar-copilot/settings/secrets/actions/new. Type the
   name `CRON_SECRET`, ask the user to paste the **same** value, click **Add secret**. The secrets
   list should then show `CRON_SECRET`.
4. Wait about 3 minutes for Render to finish deploying (the service's Events page shows "Deploy
   live").
5. **Test**: open https://github.com/HFRANK456/uar-copilot/actions/workflows/weekly-reminders.yml →
   **Run workflow** → **Run workflow** (branch `main`). Refresh until the run finishes (a minute or two;
   the first request wakes the sleeping backend). Open the run → **remind** → **Trigger reminders** and
   report the last lines. Expected: `HTTP 200` and a line like
   `{"ran":true,"campaigns":N,"reminded":N,...}`. Meanings:
   - `"ran":false ... email isn't set up` → `RESEND_API_KEY` is missing on Render.
   - `HTTP 401` → the two `CRON_SECRET` values differ; ask the user to paste it again in both places.
   - `HTTP 503 ... CRON_SECRET is not set` → Render hasn't redeployed yet, or the key name is misspelled.
   - "CRON_SECRET isn't set in the repository's Actions secrets; skipping" → the GitHub secret is missing.
   `"campaigns":0` is normal when no campaign is in review or all were emailed in the last six days.

## Report back
For reviewer onboarding changes: sign in with a newly created, unverified test reviewer that has a
pending workspace invitation. Expect a verification explanation and **Refresh verification**. Verify
the email through the inbox (the user handles sign-in), return to UAR or click the refresh button,
and expect the invitation without restarting the backend or signing out. Joining still requires an
explicit click; then the workspace switcher should show the reviewer role. Unverified users must
never see or accept email-matched invitations.

Reply with a short table: step, expected, what you saw, pass/fail. Include the exact text of any
error, a screenshot of anything that failed, and the campaign name you used. If something failed,
say which step and stop there rather than working around it, since the point is to find problems.
