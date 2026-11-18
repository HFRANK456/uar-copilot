# Self-service verification email

The Auth0 verification-link template was enabled during the November 18 live check.
The tenant uses Auth0's built-in development email provider. Initial messages did not
arrive during two signups; dashboard resends succeeded. The exact delivery cause is
not established. This change provides an explicit resend path; it does not claim to
fix automatic email delivery or remove the need for a production email provider.

## Server setup

Create a dedicated Machine-to-Machine application in the same Auth0 tenant and
authorize the Auth0 Management API with only `update:users`, the scope required by
`POST /api/v2/jobs/verification-email`. This is a broad user-update permission at
Auth0, so obtain owner approval before granting it. The application's own route
only submits verification jobs for the validated caller's subject; it exposes no
general user-update operation and accepts no recipient from the browser.

Set backend-only Render variables:

- `AUTH0_VERIFICATION_CLIENT_ID`: the dedicated application's client ID.
- `AUTH0_VERIFICATION_CLIENT_SECRET`: its secret (never put it in Vite variables).

The existing `AUTH0_DOMAIN` determines the token and Management API endpoints.
Without these variables the resend route returns a helpful 503; existing login,
email refresh and invitations continue working. The UI must not be considered
ready for release until configuration and a live resend test are complete.

## Behavior and limits

Unverified email/password users can request a resend from the verification banner.
Auth0 determines the email address from the user ID; no address or redirect URL is
accepted. Already verified users don't trigger a job. Social and local development
identities are refused. Provider error bodies and credentials are never returned.
Success means Auth0 accepted a request, not that the inbox received it.

The cooldown is one attempt per minute per subject, including failed sends, per
server process. It resets on restart and is not shared across replicas; add shared
rate limiting before scaling horizontally. Auth0 also applies its provider limits.

## Live check

Use a new unverified database account. Click Resend verification email and confirm
Auth0 records a successful verification-email job and the inbox receives it. A
second immediate click must return a wait message. Verify the email, then refresh
verification without signing out; the workspace invitation should appear. Never
mark a user verified manually to pass this test.

Reference: https://auth0.com/docs/api/management/v2/jobs/post-verification-email
