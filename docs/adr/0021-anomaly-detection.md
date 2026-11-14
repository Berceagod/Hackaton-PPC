# ADR-0021: Meter anomalies are detected from the reading and checked against sim_label
Status: accepted (slice I)

Context: T3 asks what the stored data says about the network, and specifically to compare detected meter anomalies with the simulator's `sim_label`. The simulator (`simulator/sim.py`) plants five kinds: `voltage_sag` (170-200 V), `overvoltage` (254-270 V), `outage` (0 V), `tamper` (an event the meter reports), and `load_spike` (power multiplied by 4-8, with no event — the comment in the simulator says analytics has to notice it). `meter_readings` already stores `zone`, `voltage_v`, `sim_label` and the payload. Only `stored` and `queued` readings count; a `pending` claim is not a reading yet.

Decision: One ordered detector, the same rules in `analytics.classify_reading` and in the SQL behind `PostgresRepo.analytics_snapshot`:
- voltage under 50 V -> `outage`
- voltage under 207 V -> `voltage_sag` (EN 50160 lower edge, 0.9 x 230 V; normal noise is a gaussian around 230 V with sigma 3)
- voltage over 253 V -> `overvoltage` (the upper edge; the simulator starts at 254 V)
- event `TAMPER_COVER_OPEN` or `MAGNETIC_FIELD_DETECTED` -> `tamper`
- `active_power_kw` over 3.5 times that meter's own median power -> `load_spike`

Daily load variation in the simulator stays under about 1.6x, and a spike is 4-8x, so 3.5x sits between them. A meter with a single reading has no baseline and is not called a spike. The median includes the spike itself, which is why a handful of normal readings must exist before one spike stands out; at the simulator's 2% anomaly rate the median stays on the normal readings.

`meter_anomalies` on a zone card is how many readings the detector marked, not how many the simulator labelled. `narrative.network` states the totals and how many labels match (`detected == sim_label`). A labelled reading the detector does not confirm is a miss and does not increment the zone count.

Zone cards are always Muntenia, Banat, Dobrogea. Outage and drone counts come from `files_metadata.zone`. Drone status: `pending` when there is no video, `in_progress` when there are more outage files than videos, otherwise `completed`. Connection-document audit rows are counted in that category's verdict (`audit_logs` joined to `files_metadata`); they do not depend on which storage holds the file.

Consequences: The detector is a threshold, not a model. It will miss a load spike on a meter that has only anomalous samples, and it will not notice a tamper that the meter did not put in `events`. Both are visible in the agreement sentence.
