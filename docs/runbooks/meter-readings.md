# Runbook: meter readings (never lost, never duplicated)

Dashboards: Grafana -> *FileGrid - Business guarantees* -> Meter readings. Readings are acknowledged (202) only after they are stored or safely queued in the outbox, and a (meter, timestamp) pair can exist once.

## MeterReadingsNotArriving
**Means:** readings were arriving and none has been accepted for 2 minutes: a gap in the data.
1. Are the meters (simulator) running? `docker compose ps simulator`, `GET` the simulator's status (internal) or the *Waiting at the meters* panel.
2. Is the gateway up? `curl -k https://localhost/api/v1/livez` should answer `alive`.
3. Backend health: `docker compose ps backend`, `docker compose logs backend --since 5m`.
4. If readings are waiting at the meters (see next alert) they will be delivered when the path is back.

## MeterReadingsLostAtSender
**Means:** a meter dropped a reading: FileGrid was unreachable for longer than the meter's backlog can hold. **Those readings are gone for good** (the only way to lose one). Find why the path was down for that long, and raise the sender's backlog (`BACKLOG_MAX` on the simulator) if outages of that length are expected.

## MeterReadingsWaitingAtSender
**Means:** delivery is delayed; readings are buffered at the meters. If it keeps growing, `MeterReadingsLostAtSender` follows. Same checks as *NotArriving*; also look at the network (is the chaos console or `scripts/chaos/network-bad.sh` still active? `scripts/chaos/network-ok.sh` undoes it).

## MeterReadingServerErrors
**Means:** more than 5 % of readings fail with a server error. Meters retry, so nothing is lost yet.
1. `docker compose logs backend --since 5m | grep -i error`.
2. Storage failing? *Operations* dashboard, storage error rate. If a storage is down readings are queued instead (see *MeterOutboxBacklog*); server errors mean something else (database, bulkhead).

## MeterOutboxBacklog
**Means:** accepted readings are held in the metadata database because the meter storage is unavailable. They are safe. Restore the storage (`docker compose ps`); the outbox drains by itself, oldest first, and keeps the original trace id.

## MeterOutboxGrowing
**Means:** more than 1000 readings are queued and not draining: the storage has been down a long time. Restore it before the metadata database grows large. `docker compose exec postgres-meta psql -U filegrid -d filegrid -c "select status, count(*) from meter_readings group by 1"`.

## MeterDuplicateReadingsHigh
**Means:** many incoming readings are duplicates. They are all rejected (409) and nothing is stored twice, so there is no data risk, but a sender is replaying (retry storm, clock reset). Check the simulator's `redelivered` counter and its clock.
