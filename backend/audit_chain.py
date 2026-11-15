"""Tamper-evident audit log (improvements.md 2.3).

Every audit row stores `prev_hash` (the hash of the row before it) and `row_hash`
(SHA-256 over prev_hash and the row's own fields). The rows therefore form a chain:
changing a row breaks its own hash, deleting or inserting a row breaks the next row's
`prev_hash`. `GET /audit/verify` recomputes the whole chain.

Limits, stated plainly: deleting the LAST rows of the chain leaves a shorter but
still-consistent chain, so it is not detected by the chain alone. The head hash is
returned by the verification so it can be anchored somewhere the database owner cannot
rewrite (the scrubber logs it periodically to the log store).
"""

import hashlib
from datetime import datetime, timezone

GENESIS = "0" * 64
_SEP = "\x1f"  # unit separator: no field can contain it by accident, so no ambiguity


def _ts(value) -> str:
    if isinstance(value, str):
        return value
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def row_hash(prev_hash: str, row: dict) -> str:
    parts = [
        prev_hash,
        str(row["id"]),
        str(row["file_id"]),
        row["action"],
        row["username"],
        row["trace_id"],
        row.get("details") or "",
        _ts(row["created_at"]),
    ]
    return hashlib.sha256(_SEP.join(parts).encode("utf-8")).hexdigest()


class ChainVerifier:
    """Feed rows in chain order; `result()` says whether the chain is intact."""

    def __init__(self) -> None:
        self._expected_prev = GENESIS
        self.checked = 0
        self.head: str | None = None
        self.first_break: dict | None = None

    def feed(self, row: dict) -> None:
        prev = (row["prev_hash"] or "").strip()
        stored = (row["row_hash"] or "").strip()
        if self.first_break is None:
            if prev != self._expected_prev:
                self.first_break = {"seq": row.get("seq"), "id": str(row["id"]),
                                    "reason": "chain_broken"}  # a row was removed or inserted
            elif row_hash(prev, row) != stored:
                self.first_break = {"seq": row.get("seq"), "id": str(row["id"]),
                                    "reason": "row_hash_mismatch"}  # the row was edited
        self.checked += 1
        self._expected_prev = stored
        self.head = stored

    def result(self) -> dict:
        return {
            "valid": self.first_break is None,
            "checked": self.checked,
            "head_hash": self.head,
            "first_break": self.first_break,
        }
