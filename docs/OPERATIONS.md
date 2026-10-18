# Operations runbook

Referenced by `ballot/migrations/0002_seed_legacy.py`.

## 1. What must be backed up (the set that preserves decryptability)

A database dump **alone is not a complete backup**: ballots are encrypted
with a key that lives outside the database, and voter credentials are
peppered with a secret that also lives outside it.

| Item | Location | Consequence if lost |
| --- | --- | --- |
| PostgreSQL dump (or `db.sqlite3`) | DB | total data loss |
| Ballot encryption key | `BALLOT_ENCRYPTION_KEY` env or `secrets/ballot.key` | sealed ballots permanently undecryptable → all count as spoiled |
| Result signing key (Ed25519 PEM) | `RESULT_SIGNING_KEY` env or `secrets/result_signing.key` | old snapshots can no longer be re-verified by the server; publish the **public key** now (see §5) |
| Voter token pepper | `VOTER_TOKEN_PEPPER` env | every outstanding voter credential stops verifying (already-cast ballots are unaffected) |
| Issued-credential CSVs | `private/credentials/<election>.csv` | operators can no longer reissue lost voter tokens |

Store the key material in a different place than the DB dump (otherwise the
"database thief" threat in `docs/BALLOT_SECRECY.md` is moot).

### Backup commands

```bash
# PostgreSQL (daily, plus one before every migration/upgrade)
docker compose exec db pg_dump -U voting -Fc voting > votesys-$(date +%F).dump

# SQLite (local/small deployments)
cp db.sqlite3 votesys-$(date +%F).sqlite3

# Keys + credentials (encrypt before off-site storage!)
tar czf votesys-keys-$(date +%F).tgz secrets private/credentials
```

### Restore

```bash
docker compose exec -T db pg_restore -U voting -d voting --clean --if-exists < votesys-YYYY-MM-DD.dump
# then copy secrets/ and private/ back into place and restart
```

After a restore, verify at least one election's audit chain (§4) before
serving traffic.

## 2. Key custody rules

* Generate keys on the production host or in the secret manager; never in
  the repository (`.gitignore` already excludes `secrets/`, `*.pem`, `*.key`).
* **Do not rotate `BALLOT_ENCRYPTION_KEY` or `VOTER_TOKEN_PEPPER` while
  sealed ballots or unexpired credentials exist** — rotation is
  indistinguishable from mass spoilage / mass credential revocation.  Both
  rotations are intentionally not offered as an online operation.
* The signing key can be rotated per election cycle: publish the Ed25519
  **public key** (`audit.keys.public_key_pem()`) alongside certified results
  so third parties can verify exports offline.
* Losing `RESULT_SIGNING_KEY` in HMAC fallback mode (raw-string key) is
  worse than in Ed25519 mode: HMAC signatures cannot be verified without the
  secret.  Prefer PEM keys in production (`.env.example` documents this).

## 3. Routine procedures

| Task | Where |
| --- | --- |
| Create organizations / assign roles | Django shell (no UI yet) — see `README.md` status table |
| Register voters | Dashboard → Election → Voters (single add or pasted CSV, max 5000 rows) |
| Issue voter credentials | Voter access point (one-time display) or freeze configuration → `private/credentials/<election>.csv` |
| Open / close an election | Dashboard action buttons (auto also happens by schedule) |
| Reopen, approve configuration, decertify | Four-eyes: one user files the approval request with a reason, a **different** eligible approver decides |
| Recount after close | Results page → Recount (official only, reason recorded, audit-chained) |
| Certify | Lifecycle: CLOSED → RECONCILIATION → REVIEW → CERTIFIED (certification re-verifies reconciliation live; blocks with `reconciliation_failed`/`snapshot_stale` if the box or tally drifted) |
| Export results | Results page → Export JSON (signed, deterministic) |
| Export audit chain | Monitor → Audit export (privileged roles only; the export itself is audit-chained) |

## 4. Verifying integrity (do this after upgrades and before publishing)

1. **Audit chain**: Dashboard → Election → Audit → "Verify chain"
   (or `GET /elections/<id>/audit/export/…` and recompute).  The hash chain
   covers every mutation; a broken chain means tampering or a partial
   restore — investigate before proceeding.
2. **Reconciliation**: the RECONCILIATION state must hold
   `ballot-box last_seq == accepted ballots == valid + invalid ballots ==
   voters who voted`.  Certification refuses to run if it does not.
3. **Tally**: `/elections/<id>/results/v<n>/verify/` recomputes counts from
   sealed ballots and checks the snapshot signature; also available as JSON
   for third parties.
4. In Python: `audit.services.verify_all_chains()` returns per-chain results.

## 5. Incident notes

* **Suspected ballot-box tampering**: certification blocks
  (`reconciliation_failed`); the discrepancy is itself evidence — take a DB
  backup before touching anything, then compare `BallotBox.last_seq`, the
  accepted-ballot count and the snapshot.
* **A certified result must be retracted**: decertify (four-eyes).  The old
  snapshot is superseded, never deleted, and the supersession is
  audit-chained.
* **Lost voter token**: reissue at the voter access point while the
  election is in registration/open — old authorization is revoked, nothing
  links the new token to a ballot.
* **Disk exposure of `private/credentials/`**: those CSVs contain live
  tokens; treat their exposure as credential compromise and revoke/reissue
  affected elections' credentials.

## 6. Upgrades

1. Backup (§1).
2. Deploy new code; `migrate` runs in the container entrypoint (or manually).
   The legacy-data conversion migrations (`ballot/0002`, `elections/0003`)
   are one-way; reversing them is lossy — restore from backup instead.
3. Verify (§4).
