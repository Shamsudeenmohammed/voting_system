# Workstation Registration and Access Mode

## Overview

Workstations are **registered, managed devices** with a server-side identity and
a hashed device credential.  Privileges come from the database record, not from
URLs, query parameters, cookies set by the client, browser local storage or
JavaScript flags.

Purposes:

- `VOTING`: voter login using Voter ID + token, ballot navigation, review,
  submission, and automatic return to voter login 3 seconds after the backend
  confirms acceptance.
- `VERIFICATION`: authorized officer login, Voter ID lookup, eligibility and
  voting-status check, controlled token issuance only.  No ballot or candidate
  selections are exposed.

## Super Admin setup

1. Log in as the Super Admin at `/watch/` and open `/manage/control/`.
2. Click **Workstations** (or open `/workstation/manage/`).
3. Register a workstation:
   - unique `name` and `code`
   - assigned `election` (optional but recommended)
   - assigned `polling_station`
   - `purpose` (Voting or Verification)
   - a strong `device credential` (stored hashed, never in plaintext)
4. Activate/disable/revoke as needed.  Revocation is immediate: the device
   can no longer authenticate.

Workstations are also manageable through the Django admin (`/watch/`).

## Opening the correct entry point

- Voting kiosk: `/workstation/voting/` (alias `/kiosk/`) — only registered,
  active `VOTING` workstations.
- Verification console: `/workstation/verification/` — only registered, active
  `VERIFICATION` workstations with an authorized officer session.
- Workstation login: `/workstation/login/`.

A workstation must authenticate first (code + credential).  The server creates
a session only after validation; privileges are re-checked on every request and
against the (possibly revoked) database record.

## Verification workstation backdrop

The verification console shows a semi-transparent white veil over a background
picture so the form stays readable. To use any picture:

1. Put the image in `static/img/`, for example `static/img/station.jpg`.
2. Point `VERIFICATION_BACKGROUND_IMAGE` at it, either in
   `config/settings/local.py` or as an environment variable:

   ```bash
   VERIFICATION_BACKGROUND_IMAGE=img/station.jpg
   ```

   Absolute paths and full URLs also work (for example
   `/media/backgrounds/station.jpg`). Run `collectstatic` when serving through
   WhiteNoise.

The bundled default is `static/img/workstation-background.svg`. You can replace
that file in place and keep the same setting. The veil strength is the
`--verification-veil` value at the top of
`templates/elections/workstation_verification.html` if you need more or less
transparency.

## Kiosk / OS configuration

- Configure the browser/OS to open the correct entry point in fullscreen/kiosk
  mode.
- Do not pass workstation identity via URL query parameters or JavaScript.
- For stronger device binding, deploy a reverse proxy that injects
  `X-Workstation-Code` and `X-Workstation-Credential` headers from a
  server-side secret store, or use client certificates / OS-level kiosk
  accounts.  Browser-only identification cannot prove device identity: cookies
  and local storage can be copied, cleared or tampered with, and any client
  can change the URL.  The server therefore always re-validates the device
  credential and revocation status.

## Normal online voting

Online voters continue to use `/elections/<id>/access/` and `/ballot/<token>/`
without any workstation restriction.  Polling-station device checks apply only
to `/workstation/*` and `/kiosk/*` routes.

## Required migration

- `elections/migrations/0007_workstation.py` (creates the `Workstation` table).

Run `python manage.py migrate` to apply.  Existing records are preserved.