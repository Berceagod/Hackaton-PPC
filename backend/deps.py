"""Application dependencies container (injected so tests can use fakes)."""

from dataclasses import dataclass, field

from config import Settings
from policies import PolicyStore
from repo import Repo


@dataclass
class Deps:
    settings: Settings
    repo: Repo
    storages: dict  # name -> StorageBackend
    policies: PolicyStore
    # Meter readings get their own connection pool (bulkhead, slice G): a storm of
    # readings cannot take the connections the UI categories need. Same database.
    meter_repo: Repo | None = None
    bulkheads: object | None = field(default=None)

    def meters(self) -> Repo:
        return self.meter_repo or self.repo
