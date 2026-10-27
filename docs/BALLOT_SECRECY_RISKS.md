# Ballot Secrecy: Remaining Risks and Mitigations

## Implemented Protections

1. **Identity/Selection Separation**: The `Ballot` model contains no voter, authorization, or voter_id foreign keys. Ballot selections are encrypted at rest with Fernet (AES-128-CBC + HMAC).

2. **Peppered Token Hashing**: Voter access tokens are never stored in plaintext. Only a peppered HMAC-SHA256 hash is persisted, preventing offline verification of guessed tokens even if the database is compromised.

3. **Audit Event Sanitization**: The `ballot_accepted` audit event intentionally contains neither voter, authorization, receipt, submission id, nor IP hash. Only aggregate identifiers (box_seq, ballot_id, ballot_hash) are recorded.

4. **Preparation Deletion**: `BallotPreparation` records (linking authorization to submission_id) are deleted inside the accepting transaction, destroying the only durable path from voter to ballot.

5. **Token Consumption**: Tokens are consumed atomically with ballot acceptance via conditional `UPDATE ... WHERE consumed_at IS NULL`, preventing reuse.

## Remaining Risks

### 1. Timing Correlation
- **Risk**: An observer with access to server logs or network traffic may correlate the timestamp of a voter's login with the timestamp of a ballot acceptance, potentially linking identity to ballot.
- **Mitigation**: The `ballot_accepted` audit event does not include IP hash or timestamp precision beyond the database's default. However, the `accepted_at` field on the `Ballot` row is recorded.
- **Residual**: If an adversary has access to both the voter login audit trail and the ballot acceptance timestamps, correlation is theoretically possible. This requires compromise of the audit subsystem and access to ballot metadata.

### 2. Log Correlation
- **Risk**: Application logs (Django request logging, web server access logs) may record request timestamps, IP addresses, and URL paths (including token values in the ballot URL).
- **Mitigation**: The ballot token appears only in the first request URL. The `robots.txt` disallows ballot crawling. Django's request logger is set to WARNING level for `django.request`.
- **Residual**: Web server access logs (nginx, Apache) may still record full URLs including tokens. Deployments should configure access log redaction for `/ballot/` paths. The `VOTER_TOKEN_PEPPER` should be set in production.

### 3. Concurrent Request Correlation
- **Risk**: If a voter opens the ballot in multiple tabs and submits from both, the second submission is rejected as a duplicate. An observer seeing two rapid ballot requests from the same IP could infer a retry.
- **Mitigation**: Idempotent retries with the same `submission_id` return the original receipt without creating a second ballot. The duplicate vote message is uniform.

### 4. Polling Station Device Sharing
- **Risk**: In polling station mode, multiple voters may use the same approved device. The device's IP address appears in all login attempts.
- **Mitigation**: Rate limiting is per IP + election. The `ballot_accepted` audit event does not include IP hash. However, web server logs may record the shared IP.
- **Residual**: Physical polling station procedures (voter privacy booths, session clearing) are required to mitigate this at the operational level.

### 5. Token Delivery Channel
- **Risk**: Tokens issued via the `voter_access` page are displayed on screen. If the delivery channel (email, SMS, printed slip) is compromised, tokens can be intercepted.
- **Mitigation**: Tokens are single-use and expire per election policy. The `voter_access` page is restricted to election officials. Tokens are shown exactly once.
- **Residual**: Out-of-band delivery security depends on the deployment's chosen channel.

## Recommendations for Deployments

1. Set `VOTER_TOKEN_PEPPER` to a strong, unique value in production.
2. Configure web server access logs to redact `/ballot/` paths.
3. Use HTTPS for all voter-facing pages.
4. Clear browser sessions between voters at polling stations.
5. Monitor for repeated failed login attempts as potential brute-force attacks.
6. Consider using short-lived tokens (lower `token_expiry_hours`) for high-security elections.