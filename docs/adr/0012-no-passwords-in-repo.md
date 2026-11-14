# ADR-0012: No password in the repository, including the UI demo accounts
Status: accepted (slice F)

Context: PDF §4 "no password or key is in the repository". The UI account passwords were the literal words `dispecer` and `admin`, written in `start.sh`, `start.ps1`, the login screen and four demo scripts. That is a password in the repository.

Decision:
- `start.sh` / `start.ps1` generate random 16-character passwords for `dispecer@ppc.ro` and `admin@ppc.ro` into `.env` (git-ignored) and print them when the stack is up. Existing installations that still have the old literal values get new ones on the next start.
- Demo scripts read the passwords from `.env` through `scripts/lib/env.sh` and never print them.
- The login screen shows the demo passwords only in mock mode (where they belong to the in-browser mock, not to the real system) and otherwise says where the real ones are.
- `scripts/security/secrets-scan.sh` checks that secret files are ignored and untracked, and runs gitleaks (pinned container) over the whole history and over the files that would be committed now. It was shown to catch a planted token.
- nginx limits `/api/v1/auth/login` to 20 attempts per minute per IP (burst 10, then 429) against password guessing.

Consequences: logging in needs the password printed by the start script (or read from `.env`). Mock-mode passwords remain in the frontend source; they open nothing outside the browser.
