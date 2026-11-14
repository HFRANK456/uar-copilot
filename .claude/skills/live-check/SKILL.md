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
1. Open https://uar-copilot.vercel.app. It should land on **Campaigns** (the URL ends in `/campaigns`).
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
7. **Start review**. You're the campaign owner, not the assigned reviewer, so each item says
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
9. **Remediation**: **Download tickets (CSV for Jira import)** downloads a CSV. On jordan.kim, enter
   ticket `IT-101`, **Save ticket**, **Mark done** → "Done, not verified". In **Verify with a fresh
   access list**, choose **AWS** and `data/sample_app_access_aws_after_remediation.csv`, then
   **Check changes** → "AWS: 1 of 2 change(s) confirmed" and "Still there: contractor.ext …";
   jordan.kim shows **Verified**. **Close campaign** must still be disabled. Then **Accept without
   upload…** on contractor.ext and ghost-user with the reason "Live check: removal confirmed by the
   vendor by email" → "3 of 3 confirmed". **Close campaign** → badge **Closed**.
10. **Download evidence pack** → a file `evidence-Live_check_….zip` downloads. **Show events**:
   the newest event is `evidence.exported`, and the header says "Hash chain verified".
11. Open **Quick scan** in the nav: the page must say "Nothing is saved".

## Report back
Reply with a short table: step, expected, what you saw, pass/fail. Include the exact text of any
error, a screenshot of anything that failed, and the campaign name you used. If something failed,
say which step and stop there rather than working around it, since the point is to find problems.
