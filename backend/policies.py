"""Per-category policy engine: DB-backed, hot-reloadable, shared across replicas.

policies.yaml seeds the defaults on first start; afterwards the `policies_config`
table is the source of truth. `PUT /admin/policies` writes it; every backend
replica re-reads it at most `ttl` seconds later, so no restart or redeploy is needed.
"""

import logging
import time
from dataclasses import dataclass, replace

import yaml

log = logging.getLogger("filegrid.policies")

CATEGORIES = ("meter_readings", "outage_records", "connection_docs", "drone_videos")
STORAGES = ("postgres", "s3", "ftp")
PRIORITIES = ("high", "normal", "low")


class PolicyError(ValueError):
    """Invalid policy document (maps to HTTP 422)."""


@dataclass(frozen=True)
class CategoryPolicy:
    storage: str
    encryption: bool
    deduplicate: bool
    priority: str
    # one extra copy on a DIFFERENT storage (improvements.md 2.2); 0 = none. Optional in the
    # document: the UI does not send it, and an update without it keeps the current value.
    replicas: int = 0
    replica_storage: str | None = None  # None = pick the first other storage automatically
    # automatic failover (improvements): where NEW files go while the primary storage is down
    # or its category is overloaded. None = no failover. Never a storage equal to the primary.
    fallback_storage: str | None = None

    def to_dict(self) -> dict:
        data = {
            "storage": self.storage,
            "encryption": self.encryption,
            "deduplicate": self.deduplicate,
            "priority": self.priority,
        }
        # extras appear only when switched on: the documented policy shape stays four fields
        if self.replicas:
            data["replicas"] = self.replicas
        if self.replicas and self.replica_storage:
            data["replica_storage"] = self.replica_storage
        if self.fallback_storage:
            data["fallback_storage"] = self.fallback_storage
        return data


def parse_category(name: str, cfg) -> CategoryPolicy:
    if not isinstance(cfg, dict):
        raise PolicyError(f"{name}: expected an object")
    missing = {"storage", "encryption", "deduplicate", "priority"} - set(cfg)
    if missing:
        raise PolicyError(f"{name}: missing {sorted(missing)}")
    if cfg["storage"] not in STORAGES:
        raise PolicyError(f"{name}: storage must be one of {STORAGES}")
    if cfg["priority"] not in PRIORITIES:
        raise PolicyError(f"{name}: priority must be one of {PRIORITIES}")
    for flag in ("encryption", "deduplicate"):
        if not isinstance(cfg[flag], bool):
            raise PolicyError(f"{name}: {flag} must be true or false")
    replicas = cfg.get("replicas", 0)
    if isinstance(replicas, bool) or replicas not in (0, 1):
        raise PolicyError(f"{name}: replicas must be 0 or 1")
    replica_storage = cfg.get("replica_storage")
    if replica_storage is not None:
        if replica_storage not in STORAGES:
            raise PolicyError(f"{name}: replica_storage must be one of {STORAGES}")
        if replica_storage == cfg["storage"]:
            raise PolicyError(f"{name}: the replica must be on a different storage than the primary")
    if name == "meter_readings" and replicas:
        raise PolicyError("meter_readings cannot have replicas (exactly-once ingest, tiny files)")
    fallback = cfg.get("fallback_storage")
    if fallback is not None:
        if fallback not in STORAGES:
            raise PolicyError(f"{name}: fallback_storage must be one of {STORAGES}")
        if fallback == cfg["storage"]:
            raise PolicyError(f"{name}: the fallback must be a different storage than the primary")
    return CategoryPolicy(cfg["storage"], cfg["encryption"], cfg["deduplicate"], cfg["priority"],
                          replicas, replica_storage, fallback)


def parse_policy_document(doc) -> dict[str, CategoryPolicy]:
    """Accepts the frontend map {category: policy} or the YAML file shape {categories: {...}}."""
    if isinstance(doc, dict) and isinstance(doc.get("categories"), dict):
        doc = doc["categories"]
    if not isinstance(doc, dict) or not doc:
        raise PolicyError("policy document must be a non-empty object")
    unknown = set(doc) - set(CATEGORIES)
    if unknown:
        raise PolicyError(f"unknown categories: {sorted(unknown)}")
    return {name: parse_category(name, cfg) for name, cfg in doc.items()}


class PolicyStore:
    def __init__(
        self,
        policies: dict[str, CategoryPolicy],
        persistence=None,  # object with async load_policies()/save_policies()
        ttl: float = 2.0,
    ) -> None:
        self._policies = dict(policies)
        self._persistence = persistence
        self._ttl = ttl
        self._loaded_at = 0.0

    @classmethod
    def from_yaml(cls, path: str, persistence=None, ttl: float = 2.0) -> "PolicyStore":
        with open(path, encoding="utf-8") as fh:
            defaults = parse_policy_document(yaml.safe_load(fh))
        return cls(defaults, persistence, ttl)

    async def load(self) -> None:
        """Startup: adopt persisted policies, or seed the DB from the YAML defaults."""
        if self._persistence is None:
            return
        stored = await self._persistence.load_policies()
        if stored:
            self._policies.update(parse_policy_document(stored))
        else:
            await self._persistence.save_policies(self.as_dict_sync())
        self._loaded_at = time.monotonic()

    async def _refresh(self) -> None:
        if self._persistence is None or time.monotonic() - self._loaded_at < self._ttl:
            return
        try:
            stored = await self._persistence.load_policies()
            if stored:
                self._policies.update(parse_policy_document(stored))
        except Exception:  # keep serving the last known policy if the DB blips
            log.warning("policy refresh failed; using cached policy", exc_info=True)
        self._loaded_at = time.monotonic()

    def invalidate(self) -> None:
        """Another worker or replica saved a change (Postgres NOTIFY): re-read on next use.
        The TTL refresh stays as a fallback if the notification connection drops."""
        self._loaded_at = float("-inf")

    async def get(self, category: str) -> CategoryPolicy:
        await self._refresh()
        return self._policies[category]

    async def all(self) -> dict[str, dict]:
        await self._refresh()
        return self.as_dict_sync()

    def as_dict_sync(self) -> dict[str, dict]:
        return {name: p.to_dict() for name, p in self._policies.items()}

    async def update(self, doc) -> dict[str, dict]:
        changes = parse_policy_document(doc)
        raw = doc["categories"] if isinstance(doc.get("categories"), dict) else doc
        for name, cfg in raw.items():  # an update that does not mention replicas keeps them
            old = self._policies.get(name)
            if old is not None and "replicas" not in cfg:
                keep = old.replica_storage if old.replica_storage != changes[name].storage else None
                changes[name] = replace(changes[name], replicas=old.replicas, replica_storage=keep)
            if old is not None and "fallback_storage" not in cfg:  # not mentioned: keep it
                keep = old.fallback_storage if old.fallback_storage != changes[name].storage else None
                changes[name] = replace(changes[name], fallback_storage=keep)
        merged = {**self._policies, **changes}
        if self._persistence is not None:
            await self._persistence.save_policies(
                {name: p.to_dict() for name, p in merged.items()}
            )
        self._policies = merged
        self._loaded_at = time.monotonic()
        return self.as_dict_sync()
