# Ballot secrecy: model and limitations

Referenced by `ballot/crypto.py`.  This document states what the system does
and — just as important — what it does **not** provide.  No claim here goes
beyond what the code and tests demonstrate.

## What keeps the ballot secret

1. **No durable link between voter and ballot.**
   `BallotAuthorization` (who may vote) and `Ballot` (what was voted) are
   separate rows in separate tables with no foreign key or shared identifier.
   A ballot stores an encrypted selection blob, a random receipt code and the
   ballot-box sequence number — never a voter reference, token or IP.
2. **Single-use credentials, hash-only storage.**
   Voter tokens are looked up by peppered HMAC-SHA256 (`ballot/crypto.py`);
   the plaintext token exists only at issuance (shown once, or written to the
   operator-distributed credentials CSV).  A stolen database alone does not
   reveal usable tokens without the pepper.
3. **Short-lived preparation capability.**
   The ballot form is authorized by a one-time `BallotPreparation` that is
   deleted inside the acceptance transaction; a leaked page URL stops working
   after one successful cast.
4. **Exactly-once, transactional acceptance.**
   `select_for_update()` on the per-election `BallotBox` row assigns the
   sequence; duplicate submissions resolve to the original receipt
   (idempotency by `submission_id`) and a second credential use is rejected.
   Verified by the 14-test `ballot` suite including a threaded-concurrency
   test.
5. **Encryption at rest (defence in depth).**
   Selections are Fernet-encrypted with a server-side key.  This protects
   against database-dump disclosure, *not* anonymity — separation of the
   tables does that.
6. **Minimal, selection-free audit trail.**
   The `ballot_accepted` audit event carries only `box_seq`, `ballot_id` and
   a content hash; `audit.services.record()` actively refuses event fields
   that could contain ballot content, credentials or raw IPs.  Logging shows
   only `box_seq`.  No dashboard or export displays selections together with
   voter identity — the data to do so does not exist in one place.
7. **Public verifiability of counts, not selections.**
   Anyone can check that the tally snapshot matches a recomputation and its
   signature (`/elections/<id>/results/v<n>/verify/`); they cannot inspect individual ballots.

## Threat model summary

| Adversary | Protected? |
| --- | --- |
| Curious operator reading logs/audit/dashboard | Yes — no ballot selections and no voter↔ballot link are present there |
| Database thief (offline) | Selections encrypted; credentials only as peppered hashes; identity/content unlinkable |
| Malicious voter double-voting | Yes — single-use authorization + idempotent submission handling |
| Observer watching the voter's screen/device or shoulder-surfing | **No** — out of scope for server-side systems |
| Coercion/vote-buying ("show me your receipt") | **No** — receipt codes prove inclusion, not content; but a coerced voter can be watched while voting |
| Network attacker at the TLS layer | See limitations (metadata) |
| Server administrator with shell + all keys + DB | **No** — can read encrypted selections and re-derive everything; the audit chain and signatures are the detection mechanism, not prevention |
| Traffic analysis (who voted when) | **Partially** — the app never records voter+timestamp with a ballot, but the TLS terminator/proxy sees source IPs and request times; scrub or ship those logs per `docs/OPERATIONS.md` |

## Explicit limitations (accepted, not bugs)

* **No end-to-end client verifiability** (Helios-class): voters must trust
  the server's separation of tables; there is no mixnet or
  re-encryption-proof scheme.
* **Token delivery is out-of-band.** The system displays a credential once
  or writes a CSV for operators to distribute; it does not send email/SMS,
  and it cannot audit how a paper/slip reached the voter.
* **Idempotent replay of a lost response requires the election to still be
  open.** If the election closes (or is closed server-side) between a cast
  and the voter's retry, the retry is refused with the uniform 403 wording
  instead of replaying the receipt.  The ballot itself is still counted
  exactly once; the voter can confirm via the receipt lookup if they kept
  the receipt, or via the public results.  Reopening to recover a receipt is
  a four-eyes operation and is not warranted for this alone.
* **Reopen after close breaks "voted while open" purity**: a reopened
  election accepts new ballots until re-closed; every reopen is
  dual-controlled, counted in `reopened_count` and audit-chained.
* **Timing metadata exists at the infrastructure layer** (web-server access
  logs include IP + timestamp for `/ballot/<token>/` requests).  Production
  logging is stdout-only JSON-ish lines; operators must configure log
  retention/scrubbing — the application cannot retroactively delete
  proxy-level logs.
* **Key loss is fatal and key rotation is destructive**: losing
  `BALLOT_ENCRYPTION_KEY` makes sealed ballots permanently undecryptable
  (tally counts them as spoiled); rotating it (or `VOTER_TOKEN_PEPPER`)
  invalidates all existing sealed ballots / outstanding credentials.
  See `docs/OPERATIONS.md` for custody rules.
* **Spoiled ballots are counted, not repaired.** A ballot that fails
  decryption (e.g. a tampered ciphertext) is recorded as invalid in the
  tally and is visible as `invalid` in reconciliation — by design it cannot
  be silently dropped.
