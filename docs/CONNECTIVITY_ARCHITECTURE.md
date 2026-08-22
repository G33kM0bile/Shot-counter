# Shot Counter: Connectivity and Deployment Architecture

## Purpose

The shot-counter system must continue working if internet service becomes available at the range but the club cannot administer the local router or firewall. The design must therefore require no inbound ports, port forwarding, public IP address, or firewall exception at the range.

The range installation should be treated as an offline-capable edge device. It records and processes detections locally, then initiates secure outbound synchronization whenever connectivity is available.

## Design principles

- **Outbound connections only.** Every connection from the range is initiated by the range device over standard HTTPS.
- **Offline-first operation.** Detection and local storage continue without internet access.
- **Store and forward.** Unsynchronized records remain in a local SQLite queue until the central service acknowledges them.
- **Idempotent synchronization.** Retrying an upload or event must never create duplicate shots.
- **Minimal exposure.** SQLite, SSH, the detector, and its local management interface are never exposed directly to the public internet.
- **Central public dashboard.** The public website remains on centrally managed infrastructure and does not depend on inbound access to the range.
- **Privacy-aware presentation.** Fine-grained public statistics can be soft-reset without deleting data used by aggregate monthly, yearly, and total counters.

## Recommended topology

```text
Microphone / recorder
        |
        v
Range detector and local processor
        |
        +--> Local SQLite source and synchronization queue
        |
        +--> Outbound HTTPS synchronization
                    |
                    v
        Central ingestion service
                    |
                    +--> Central database
                    |
                    +--> Public statistics dashboard
```

The public dashboard must never connect back to the range. The range device pushes data outward when it can reach the central service.

## Connectivity options

### Primary recommendation: outbound HTTPS synchronization

The detector periodically sends signed event batches to a central HTTPS endpoint. This is the smallest and most portable solution because outbound HTTPS normally works on guest networks, carrier-grade NAT, and networks where the club has no router access.

Each range device should receive its own revocable credential. Requests should include a stable event identifier, detector identifier, recording timestamp, confidence data, and any required integrity metadata.

### Cloudflare Tunnel

Cloudflare Tunnel can publish a local administrative or ingestion service through an outbound connection without opening inbound ports. It is useful when a centrally managed service must reach an application running at the range.

For the normal shot-event flow, direct outbound synchronization is still preferable: it keeps the range device independent of a continuously available tunnel and makes offline queuing explicit.

### Tailscale

Tailscale is suitable for private administration between trusted devices. It can provide SSH or private web access without port forwarding. It should complement the synchronization API rather than become a requirement for ordinary shot processing.

## Offline synchronization

Every locally recorded event should have a unique immutable identifier. A synchronization cycle should:

1. Select a bounded batch of unsynchronized events from SQLite.
2. Send the batch to the central HTTPS endpoint.
3. Let the server insert only event identifiers it has not already accepted.
4. Return acknowledgements for stored and previously known events.
5. Mark acknowledged local rows as synchronized in one SQLite transaction.
6. Retry unacknowledged rows later with exponential backoff.

This pattern allows connections to fail at any point without losing events or counting a shot twice.

Audio uploads should use the same principle. A content hash identifies duplicate recordings, and files remain local until the central service confirms receipt or processing.

## Security boundaries

- Use TLS for all synchronization traffic.
- Give every detector a separate, revocable credential.
- Prefer short-lived tokens or mutual TLS when operationally practical.
- Do not embed infrastructure administrator credentials in the detector.
- Restrict the central ingestion endpoint to the minimum required operations.
- Validate request size, schema, timestamps, detector identity, and event identifiers.
- Apply server-side rate limits and retain an audit trail of rejected requests.
- Keep the detector, SQLite database, and processing tools unreachable from the public internet.
- Use a private overlay such as Tailscale for maintenance access.

## Data ownership

While offline, the range SQLite database is the authoritative source for unsynchronized detections. After acknowledgement, the central database becomes the source used by the public dashboard, while the range retains enough history for recovery and auditing.

The system should document retention periods for original audio, processed audio, failed uploads, event metadata, and synchronization logs.

## Privacy

Aggregate statistics may include all valid detections while short-term public views are suppressed when sensitive users are present. A privacy reset should affect only presentation cutoffs; it must not delete detections or interfere with synchronization and deduplication.

Exact event times, recent-event tables, logs, filenames, detector identifiers, and recording metadata must not be exposed through public endpoints when the privacy cutoff is active.

## Suggested rollout

1. Keep the current local detector, SQLite database, upload processor, and dashboard operational.
2. Add stable event identifiers and a synchronization-state field to local records.
3. Implement a central authenticated batch-ingestion endpoint with idempotent inserts.
4. Add an outbound synchronization worker with durable retry behavior.
5. Test extended offline operation, interrupted uploads, duplicate batches, clock errors, and reconnection.
6. Add Tailscale for private maintenance if needed.
7. Add Cloudflare Tunnel only for services that genuinely require centrally initiated access.

## Acceptance criteria

- The detector counts and stores shots with no internet connection.
- Reconnection synchronizes all pending events automatically.
- Repeated synchronization never creates duplicates.
- No inbound router or firewall configuration is required.
- The range exposes no public SSH, database, detector, or management ports.
- Revoking one device credential does not affect other devices.
- Public privacy resets do not delete source data or change long-term totals.

