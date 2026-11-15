# ADR-0033: Signed integrity receipts (Ed25519)
Status: accepted (improvements.md 2.3)

## Context
"FileGrid can prove the retrieved file is identical to the stored one" is currently a claim the user takes on trust: the proof (SHA-256, `X-Integrity-Verified`) lives in HTTP headers and logs.

## Decision
- `GET /api/v1/files/{id}/receipt` (dispatcher or admin) returns `{payload, signature, alg: "Ed25519", key_id}`. The payload holds the file's id, name, category, storage, size, SHA-256, `encrypted`, stored-at, trace id, and the result of a verification done just before signing (re-hash of the stored bytes, decrypting first when needed). `?verify=false` skips the re-hash and says so (`performed: false`).
- **A damaged file gets no receipt**: HTTP 409 and a `receipt_refused` audit entry, the same rule as a download. A receipt is therefore evidence the file was intact when issued.
- The signature is over canonical JSON (sorted keys, no whitespace, UTF-8). The signing key is derived from `FILEGRID_MASTER_KEY` with HKDF, so there is no new secret to generate, back up or lose. `GET /receipts/public-key` publishes the public key; `POST /receipts/verify` checks a receipt. Both are public on purpose: the point is that a client can verify a receipt it kept, with only the public key, without trusting this server (`scripts/demo/receipt.sh` does exactly that with the `cryptography` library alone).
- Every issued or refused receipt is written to the file's audit trail.

## Consequences
- Rotating the master key changes the signing key; older receipts verify only against the older public key (`key_id` tells them apart). Keep published public keys.
- A receipt proves the state at issue time, not forever; the background scrubber covers "later".
- Receipt issuing re-reads the file: cost proportional to size (the endpoint is for deliberate use, not bulk).
