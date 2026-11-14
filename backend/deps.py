"""Application dependencies container (injected so tests can use fakes)."""

from dataclasses import dataclass

from config import Settings
from policies import PolicyStore
from repo import Repo


@dataclass
class Deps:
    settings: Settings
    repo: Repo
    storages: dict  # name -> StorageBackend
    policies: PolicyStore
