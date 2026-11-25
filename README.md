# SeaweedFS replica inventory for InfluxDB 3 Enterprise

⚡ scheduled
🏷️ monitoring, inventory
🔧 InfluxDB 3 Enterprise

[![Tests](https://github.com/rkinginflux/plugin-seaweedfs/actions/workflows/tests.yml/badge.svg)](https://github.com/rkinginflux/plugin-seaweedfs/actions/workflows/tests.yml)

## Description

A read-only, single-file Python scheduled plugin that records SeaweedFS
normal-volume replica locations and policy metadata in InfluxDB 3 Enterprise.

Use it to answer: “Which node, rack, and data center holds this volume, and what
replication policy and compact revision does that replica report?”

This is an independent community project, not an official InfluxData or
SeaweedFS integration.

## Complements Telegraf

Continue using Telegraf `inputs.prometheus` for SeaweedFS's native metrics:
under-replication and mismatch gauges, writable/crowded volumes, capacity,
bytes, request/error counters, latency, S3/filer telemetry, and process health.

This plugin fills a different gap: the complete normal-volume → replica →
node/rack/data-center/policy relationship.
It does not re-export native monitoring counters.
A replica's read-only attribute is diagnostic metadata, not another aggregate
read-only gauge.
Snapshot row counts are integrity metadata, not replacement volume-count gauges.

## Configuration

### Plugin metadata

The JSON docstring in `seaweedfs_inventory.py` declares the scheduled trigger
and all seven arguments for InfluxDB 3 Explorer.
Only scheduled triggers are supported; TOML configuration is not implemented.

Trigger argument maps use comma-separated `key=value` pairs; values are strings.
Unknown keys are rejected.

### Required parameters

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `cluster` | string | required | Stable logical cluster identity. Use one collector per cluster. |
| `master_url` | string | required | HTTP(S) origin only: no path, query, fragment or embedded credentials. |

### TOML configuration

TOML files and `config_file_path` are not supported.
Use the documented trigger argument map.

### Optional parameters

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `timeout_seconds` | number | `5` | HTTP-operation deadline, 0.1–30 seconds per request; DNS caveat below. |
| `max_response_bytes` | integer | `8388608` | Hard per-response body limit, 1024–52428800 bytes. |
| `max_records` | integer | `10000` | Hard raw replica-record limit, 1–100000. Exact duplicate inputs count toward this cap. |
| `ca_file` | path | unset | CA bundle path present on every eligible process node. TLS verification stays enabled. |
| `bearer_token_env` | string | unset | Name of an existing process environment variable containing a reverse-proxy bearer token. Requires HTTPS. Never pass the token itself. |

For custom TLS trust, add `ca_file=/plugins/seaweedfs-ca.pem` to both trigger
and test arguments.
For proxy bearer authentication, also add
`bearer_token_env=SEAWEEDFS_MONITOR_TOKEN`, having provisioned that environment
variable securely on every eligible process node.
This is proxy bearer support, not automatic SeaweedFS credential discovery or
mutual TLS.

Each poll performs only `GET /cluster/status` and `GET /vol/status`.
A known leader is required; leader strings and node IDs are never dialed.
There is no automatic discovery, proxy-environment use, retry/failover, or
redirect following.
Administrative GET routes are explicitly allowlisted because other SeaweedFS GET
routes can mutate state.

A monotonic deadline covers TCP attempts, TLS handshake, request sending,
headers and body reads.
Every raw read uses remaining time, preventing trickled headers from resetting
the timeout.
OS DNS resolution is not guaranteed to honor socket timeouts; the deadline is
checked after resolution.
CA loading, JSON decoding and local record processing are also outside the HTTP
deadline.
This is not a hard real-time total plugin-runtime guarantee.

The master API has no pagination here.
Caps fail the whole poll rather than publishing partial data; post-fetch
filtering would not reduce master work.
Use a 60–300-second cadence and retention appropriate to replica count.
Maximum configurable limits are safety bounds, not tested scale guarantees.

### Parameter examples

The trigger setup uses `cluster=example-seaweedfs` and
`master_url=https://seaweedfs-master.example.com:9333`.
For optional limits, use `timeout_seconds=10`, `max_response_bytes=16777216`,
and `max_records=20000` in the same argument map.
The CA and bearer environment-variable examples above apply to both tests and
scheduled triggers.

## Requirements

### Software requirements

- InfluxDB 3 Enterprise with the Processing Engine enabled, a writable plugin
  directory, and a compatible `influxdb3` CLI.
- An appropriate token for plugin upload and trigger administration; uploads
  require admin access. Supply it through `INFLUXDB3_AUTH_TOKEN`, not committed
  files or command-line token arguments.
- Network access from eligible DB3 process nodes to the SeaweedFS master HTTP(S)
  API.
- SeaweedFS HTTP administration enabled and any required network whitelist/proxy
  authorization configured.
- Only one scheduled collector per logical SeaweedFS cluster.

Production plugin dependencies are Python standard library only.
DB3 injects `LineBuilder`; no SDK package import or pip installation is
required.

### Data requirements

The scheduled collector fetches its own observations.
You do not need to write input rows to InfluxDB before running it.

## Installation steps

Clone this repository on the machine running your compatible CLI:

```bash
git clone https://github.com/rkinginflux/plugin-seaweedfs.git
cd plugin-seaweedfs
export INFLUXDB3_HOST_URL=https://db3.example.com:8181
export MASTER_URL=https://seaweedfs-master.example.com:9333
export CLUSTER=example-seaweedfs
# Set INFLUXDB3_AUTH_TOKEN securely through your environment/secret manager.
# Never commit it or use shell tracing while handling credentials.
```

Replace the example endpoints. `MASTER_URL` must be reachable from the DB3
process nodes, not merely the CLI machine.
A workstation port-forward is not an in-cluster endpoint.
Plain HTTP can be used on an appropriately trusted private network without a
bearer token; prefer authenticated TLS where available.

## Trigger setup

Create a dedicated database and upload the plugin with its trigger initially
disabled:

```bash
influxdb3 create database --retention-period 7d seaweedfs_inventory
influxdb3 create trigger \
  --database seaweedfs_inventory \
  --path "$PWD/seaweedfs_inventory.py" --upload --disabled \
  --trigger-spec every:60s \
  --trigger-arguments "cluster=$CLUSTER,master_url=$MASTER_URL" \
  seaweedfs_inventory
influxdb3 query --database seaweedfs_inventory \
  'SELECT * FROM system.processing_engine_triggers'
```

For an existing installation, inspect the database and trigger first; do not
blindly recreate resources.
For a different CLI release, inspect `influxdb3 create trigger --help` before
proceeding.
Keep plugin files and optional CA files available on every eligible process
node.
Do not assume `--node-spec` pins execution on releases where it is
deprecated/ignored, and do not use `--run-asynchronous` for this collector.

### Test before enabling

```bash
influxdb3 test schedule_plugin --database seaweedfs_inventory \
  --schedule '*/30 * * * * *' \
  --input-arguments "cluster=$CLUSTER,master_url=$MASTER_URL" \
  seaweedfs_inventory.py
```

The filename refers to the uploaded server-side file.
The test runs immediately with a calculated schedule time, makes real outbound
HTTP calls, and captures/validates writes without persisting them.

Require all of the following:

- `errors: []`;
- a poll manifest with `success=t`;
- exactly `record_count` inventory lines, all sharing its timestamp and
  `snapshot_id`.

An empty engine errors array alone does not prove collection succeeded: handled
failures return `success=f`.
A zero-count success means an explicitly empty observed inventory, not proof of
data health.

### Enable and verify persisted data

```bash
influxdb3 enable trigger \
  --database seaweedfs_inventory seaweedfs_inventory
influxdb3 query --database seaweedfs_inventory \
  'SELECT * FROM system.processing_engine_triggers'
# Wait at least one full interval before checking initial writes.
influxdb3 query --database seaweedfs_inventory "$(cat queries/latest_poll.sql)"
influxdb3 query --database seaweedfs_inventory \
  "$(cat queries/latest_inventory.sql)"
```

Check `disabled=false`, fresh observation timestamps, `success=true`, and a
reconciled inventory.
Enabled/Ready alone is not proof of delivery.
Until the first successful nonempty poll creates its table, the inventory query
can report table-not-found; inspect `SHOW TABLES`, poll status and
processing-engine logs.

## Example usage

### Query a scheduled poll

After the first scheduled interval, inspect the most recent poll:

```bash
influxdb3 query --database seaweedfs_inventory \
  "SELECT success, record_count FROM seaweedfs_inventory_poll
   ORDER BY time DESC LIMIT 1"
```

### Expected output

For a successful poll containing two replicas, the selected values are:

```text
success | record_count
true    | 2
```

The count varies with the observed topology.
A handled collection failure instead records `success=false` and `error_type`;
it omits `record_count` rather than fabricating an empty inventory.

### Parse a synthetic response locally

This example requires no server and performs no network calls:

```bash
python3 - <<'PY'
import json
from pathlib import Path
from seaweedfs_inventory import parse_inventory

fixture = Path("tests/fixtures/volume_status.json")
rows = parse_inventory(json.loads(fixture.read_text()))
print("Replica records:", len(rows))
PY
```

Expected output for the bundled synthetic fixture:

```text
Replica records: 7
```

The returned rows contain volume, node, rack, data-center, and policy metadata.
Malformed documents raise `ValueError` instead of returning partial inventory.
See [Tables and snapshot consistency](#tables-and-snapshot-consistency) before
interpreting query results.

## Tables and snapshot consistency

### Schema requirements

The plugin creates the tables below through buffered writes.
No pre-existing input table is required.

### `seaweedfs_volume_replica_inventory`

Tags: `cluster`, `slot`.

Fields:

- unsigned integers: `volume_id`, `disk_id`, `compact_revision`,
  `format_version`;
- strings: `snapshot_id`, `collection`, `data_center`, `rack`, `node_id`,
  `replication`, `disk_type`, `remote_storage_name`, `source_version`;
- booleans: `read_only`, `read_only_can_delete`.

`slot` is a bounded, zero-based discriminator within a sorted snapshot, NOT a
persistent replica identity.
It prevents same-timestamp rows from collapsing without making volume IDs or
random snapshot IDs tags.
Never rebuild inventory using “latest by slot.” The actual identity is
`(cluster, data_center, rack, node_id, volume_id, disk_id)`.

Node IDs are opaque; they do not establish dialable addresses or physical-host
anti-affinity.
Raw empty `disk_type` is the source's default-HDD representation.
A present empty policy object `{}` means `000`; null or missing policy is
invalid, never a healthy default.
Replication strings describe DC/rack/node policy digits, not a policy-compliance
verdict.

### `seaweedfs_inventory_poll`

Tag: `cluster`.

Each poll has `snapshot_id`, `success` and an observation timestamp.
Successful polls also have unsigned `record_count` and string `source_version`;
failed polls have string `error_type`, without a fabricated count.
Error logging excludes exception messages, credentials and raw topology.

The collector validates the full document, deduplicates exact identities,
rejects conflicting duplicates and validates all builders before buffering
inventory rows followed by a manifest with a common timestamp/ID.

DB3 buffered writes are not a distributed multi-table transaction.
Use [queries/latest_inventory.sql](queries/latest_inventory.sql): it joins rows
to the exact successful manifest and requires the observed row count to equal
`record_count`.
It selects the latest reconciled successful snapshot within ten minutes and may
fall back to an older success.
Inspect its timestamp AND [queries/latest_poll.sql](queries/latest_poll.sql);
alert on staleness or failure rather than interpreting old inventory as current
health.
Tune these example freshness windows to your needs.

Raising a Python exception can discard buffered writes.
Handled collection failures therefore buffer `success=false` and return
normally.
Engine execution success is not collection success.
Configuration errors and failure to buffer the failure marker still raise;
asynchronous write failures may prevent any observation, so monitor freshness
and server logs too.

## Update or stop

For a controlled update, review/test the new file, disable the trigger, upload
through its existing name, repeat the plugin test, then enable and verify actual
data again:

```bash
influxdb3 disable trigger \
  --database seaweedfs_inventory seaweedfs_inventory
influxdb3 update trigger --database seaweedfs_inventory \
  --trigger-name seaweedfs_inventory --path "$PWD/seaweedfs_inventory.py"
# Repeat the test, enable and persisted-data verification steps above.
```

To stop collection without deleting data, disable the trigger and read its
status back.
Do not delete the database as a routine stop operation.

## Code overview

### Files

- `seaweedfs_inventory.py`: Single-file scheduled plugin.
- `queries/latest_poll.sql`: Latest collection status.
- `queries/latest_inventory.sql`: Count-reconciled inventory query.
- `tests/fixtures/volume_status.json`: Synthetic API response.
- `tests/`: Portable parser, HTTP/TLS, and engine writer contract tests.

### Main functions

- `read_config(args)`: Validates all trigger arguments before network access.
-
  `fetch_json(base_url, path, timeout, max_bytes, ca_file=None, bearer_token=None)`
  : Fetches an allowlisted administrative GET with a deadline and body cap.
- `parse_inventory(document, max_records=10000)`: Validates and deduplicates
  normal-volume replica records.
- `process_scheduled_call(influxdb3_local, call_time, args=None)`: Buffers
  inventory and its manifest, or a failure marker.

### Logging

Success logs report the buffered replica count.
Failure logs report only the exception class; credentials and raw topology are
excluded.
Inspect poll status and freshness even when engine execution succeeds.

## Troubleshooting

### Common issues

#### Issue: Missing or invalid configuration

**Solution**: Supply `cluster` and `master_url` and check numeric limits in
[Configuration](#configuration).
Use only documented keys; unknown arguments are rejected before polling.

#### Issue: A poll records `success=false`

**Solution**: Inspect `error_type`, endpoint reachability from each process
node,
CA trust, authorization, and response schema.
Verify `/cluster/status` reports a leader and `/vol/status` fits the configured
response and record limits.
Repeat the dry run before enabling the trigger again.

#### Issue: Inventory table does not exist or data is stale

**Solution**: The inventory table appears after the first nonempty successful
poll.
Inspect `seaweedfs_inventory_poll`, trigger state, and engine logs first.
Always compare inventory timestamps with the latest poll; a reconciled older
success does not imply current collection health.

### Debugging tips

Use the engine test in [Trigger setup](#trigger-setup) and require a successful
poll manifest as well as an empty engine errors array.
Check actual persisted rows after enabling; test writes do not persist.

## Development and verification

```bash
python3 -W error::ResourceWarning -m unittest discover -s tests -v
python3 -m py_compile seaweedfs_inventory.py
```

There are no pip dependencies.
The local TLS-fixture test requires `openssl` and explicitly skips if it is
unavailable.
CI runs the suite on Python 3.11–3.13.

Public fixtures are wholly synthetic and match the SeaweedFS 4.55 response
shape; they are not exported cluster data.
Scheduled contract tests use a small recording `LineBuilder` API double, clearly
labeled as such; they do not prove actual DB3 encoding, ingestion, or runtime
compatibility.
No proprietary SDK source is included.
HTTP deadline and TLS-trust tests use real local test servers.

The underlying collector was separately exercised against SeaweedFS 4.55 and an
InfluxDB 3 Enterprise development build: real plugin dry-run execution, repeated
scheduled persistence, count-reconciled SQL, and actual failure-marker
persistence were verified.
That reference deployment had seven normal volumes on one node, all policy `000`
.
Its private topology/raw evidence is intentionally not published.
This is not a clean-install certification, universal release-compatibility
guarantee, or live multi-replica/failover test.

## Limitations

The source is the master's heartbeat-derived normal-volume map, not a
transactional disk audit.
A known leader and HTTP 200 do not prove authoritative completeness.
The plugin does not inventory EC shards, validate full fault-domain placement,
prove durability/readability, inspect checksums, infer lost volumes from ID
gaps, or repair/rebalance storage.
Revision differences can reflect concurrent compaction rather than corruption.

Verify endpoint schemas, scheduler behavior and SDK compatibility for your
actual server versions.
Use official documentation alongside this project's examples:

-
  [InfluxDB 3 Enterprise plugins](https://docs.influxdata.com/influxdb3/enterprise/plugins/)
- [SeaweedFS](https://github.com/seaweedfs/seaweedfs)
-
  [Telegraf Prometheus input](https://github.com/influxdata/telegraf/tree/master/plugins/inputs/prometheus)

## Questions/Comments

Open a question or report an issue in the
[SeaweedFS plugin issue tracker](https://github.com/rkinginflux/plugin-seaweedfs/issues)
.

## License

[MIT](LICENSE) for this project's original code and documentation.
InfluxDB, SeaweedFS and other third-party products retain their own licenses and
trademarks; this license does not grant rights to proprietary server software.
