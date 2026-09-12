# Member authentication setup

This project supports optional parent accounts for syncing saved plans and
expressing interest in an event. Public discovery does not require an account.

## Chosen first release

- Google sign-in for a familiar one-tap/redirect flow.
- Passwordless email-link sign-in as an inclusive alternative.
- A verified account may save events and mark interest.
- Comments are not enabled in the first release; they need a moderation,
  reporting and retention policy before any public posting can be accepted.

## Live configuration

- Firebase project: `small-days-ireland`.
- Web application: `Small Days Web`.
- Enabled providers: Google and passwordless email link.
- Authorised production host:
  `claude-dev-vperrod.westeurope.cloudapp.azure.com`.
- Runtime configuration:
  `/home/azureuser/.config/kidsevents/auth.env` (mode `0600`).
- Token-verification credential:
  `/home/azureuser/.config/kidsevents/firebase-service-account.json`
  (mode `0600`).
- The user-level `kidsevents-ie.service` loads the protected environment using
  its `auth.conf` systemd drop-in.

The temporary human Google Cloud credential used during setup was revoked after
the dedicated runtime identity was verified. Do not commit or copy the protected
runtime files into this repository.

## Data kept for the first release

- Provider subject and verified email for the account.
- Saved event keys and optional interest event keys.
- Account creation/update timestamps.

No child profile, date of birth, public location, direct messaging or public
follower list is part of this release.

## What is implemented

- `GET /api/auth/config` returns the Firebase browser configuration only when
  all required public configuration values exist.
- `GET`, `POST`, and `DELETE /api/member/saves` require a verified Firebase ID
  token. The server verifies it before reading or changing the SQLite store.
- The public site keeps working without a sign-in. Once signed in, existing
  browser saves are copied to the account and later save changes sync back.

## Verification completed

- The live member page receives a configured Firebase web application.
- Google sign-in reaches the Google Accounts consent screen.
- Passwordless email-link generation is accepted by Identity Toolkit.
- A temporary verified Firebase user obtained an ID token and received `200`
  from the protected member API; the test identity and database row were then
  removed.
- Missing or invalid bearer credentials continue to receive `401`.
- Configuration persists after a clean service restart.

## Required before collecting public member data

Publish a privacy notice that identifies the controller, explains saved plans
and optional interest data, provides deletion/contact routes, and keeps any
marketing consent separate from authentication.
