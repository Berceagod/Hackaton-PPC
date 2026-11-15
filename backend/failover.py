"""Automatic failover to a category's fallback storage.

A policy may name a `fallback_storage`. NEW files of that category are routed to it, with no
manual step and no restart, in two situations:
- storage_down: the primary storage's circuit breaker is open (it keeps failing), or a write
  to it just failed (the caller retries once on the fallback);
- overload: the category's own bulkhead is more than FAILOVER_LOAD_THRESHOLD full on this
  instance, i.e. the data flux is too big for the primary. The excess is absorbed by the
  fallback instead of queuing or being refused.

The decision is made per request, so the system returns to the primary by itself the moment
it is healthy and the load is back to normal. Every file records the storage it was really
written to, so downloads, verification and the scrubber work unchanged.

Each change of state raises (or resolves) an alert shown in the app's alert bell and in the
`filegrid_failover_active` gauge (Prometheus alert FailoverActive). Existing files stay where
they are: only new ones move.
"""

import logging

import metrics

log = logging.getLogger("filegrid.failover")
_last: dict[str, tuple] = {}  # category -> (active, reason): only changes are reported


def _state(storage) -> str:
    return getattr(storage, "state", "closed")


def choose(deps, category: str, policy) -> tuple[str, str | None]:
    """The storage a new file of this category should go to, and why it is not the primary."""
    primary, fallback = policy.storage, getattr(policy, "fallback_storage", None)
    if not fallback or fallback == primary or fallback not in deps.storages or primary not in deps.storages:
        return primary, None
    if _state(deps.storages[fallback]) == "open":
        return primary, None  # the fallback is down too: nothing to gain
    if _state(deps.storages[primary]) == "open":
        return fallback, "storage_down"
    bulkheads = getattr(deps, "bulkheads", None)
    if bulkheads is not None and bulkheads.utilisation(category, policy.priority) >= deps.settings.failover_load_threshold:
        return fallback, "overload"
    return primary, None


def fallback_for_retry(deps, policy) -> str | None:
    """The fallback to try once when a write to the primary has just failed."""
    fallback = getattr(policy, "fallback_storage", None)
    if fallback and fallback != policy.storage and fallback in deps.storages and _state(deps.storages[fallback]) != "open":
        return fallback
    return None


async def note(deps, category: str, policy, used: str, reason: str | None) -> None:
    """Report a change of failover state for this category (alert + gauge). Cheap when unchanged."""
    state = (reason is not None, reason)
    if _last.get(category) == state:
        return
    _last[category] = state
    metrics.failover(category, reason is not None)
    key = f"failover:{category}"
    try:
        if reason is None:
            await deps.repo.resolve_alert(key)
            log.info("failover ended", extra={"category": category, "storage": policy.storage})
            return
        why = ("storage-ul principal este indisponibil" if reason == "storage_down"
               else "fluxul de date este prea mare pentru storage-ul principal")
        await deps.repo.raise_alert(
            key, "critical" if reason == "storage_down" else "warning",
            f"Failover activ: {category} merge pe {used}",
            f"Fișierele noi din {category} sunt scrise pe {used} în loc de {policy.storage}: {why}. "
            "Revine automat când principalul este din nou sănătos.", category)
        log.warning("failover started", extra={"category": category, "from": policy.storage,
                                               "to": used, "reason": reason})
    except Exception:
        log.warning("could not record the failover alert", exc_info=True)
