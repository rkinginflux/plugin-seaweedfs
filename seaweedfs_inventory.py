"""
{
  "plugin_type": ["scheduled"],
  "scheduled_args_config": [
    {"name": "cluster", "example": "local-seaweedfs", "description": "Stable cluster identity; run only one collector per cluster", "required": true},
    {"name": "master_url", "example": "http://seaweedfs-master.example.test:9333", "description": "Explicit master HTTP(S) origin, no path or credentials", "required": true},
    {"name": "timeout_seconds", "example": "5", "description": "Per-request timeout/deadline, 0.1 to 30 seconds", "required": false},
    {"name": "max_response_bytes", "example": "8388608", "description": "Hard JSON body limit, 1024 to 52428800 bytes", "required": false},
    {"name": "max_records", "example": "10000", "description": "Hard raw replica-record limit, 1 to 100000", "required": false},
    {"name": "ca_file", "example": "/plugins/seaweedfs-ca.pem", "description": "Optional CA file on every eligible process node; verification always enabled", "required": false},
    {"name": "bearer_token_env", "example": "SEAWEEDFS_MONITOR_TOKEN", "description": "Optional environment-variable name containing proxy bearer token; HTTPS required", "required": false}
  ]
}
"""
# SeaweedFS diagnostic normal-volume inventory, not a native metrics exporter.
# Keep request/error/size/capacity/replication gauges in inputs.prometheus.

import io
import http.client
import json
import math
import os
import socket
import ssl
import time
import uuid
import urllib.parse


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('HTTP request deadline exceeded')
    return remaining


class _DeadlineReader(io.RawIOBase):
    """Apply remaining time before EVERY raw recv, including header parsing."""
    def __init__(self, sock, deadline):
        self.sock = sock
        self.deadline = deadline
        self.raw = sock.makefile('rb', buffering=0)

    def readable(self):
        return True

    def readinto(self, buffer):
        self.sock.settimeout(_remaining(self.deadline))
        return self.raw.readinto(buffer)

    def close(self):
        try:
            self.raw.close()
        finally:
            super().close()


class _DeadlineSocket:
    def __init__(self, sock, deadline):
        self.sock = sock
        self.deadline = deadline

    def sendall(self, data):
        self.sock.settimeout(_remaining(self.deadline))
        return self.sock.sendall(data)

    def makefile(self, mode):
        if mode != 'rb':
            raise ValueError('unsupported socket file mode')
        return io.BufferedReader(_DeadlineReader(self.sock, self.deadline))

    def close(self):
        self.sock.close()


class _DeadlineConnection(http.client.HTTPConnection):
    def __init__(self, host, port, deadline, context=None):
        super().__init__(host, port=port, timeout=_remaining(deadline))
        self.deadline = deadline
        self.context = context

    def connect(self):
        # getaddrinfo is an OS DNS call and cannot be bounded by socket timeout.
        # Every TCP attempt and TLS/read/write operation uses remaining time.
        addresses = socket.getaddrinfo(self.host, self.port, 0, socket.SOCK_STREAM)
        last_error = None
        for family, socktype, proto, _, address in addresses:
            sock = socket.socket(family, socktype, proto)
            try:
                sock.settimeout(_remaining(self.deadline))
                sock.connect(address)
                if self.context:
                    sock.settimeout(_remaining(self.deadline))
                    sock = self.context.wrap_socket(sock, server_hostname=self.host)
                self.sock = _DeadlineSocket(sock, self.deadline)
                return
            except Exception as exc:
                sock.close()
                last_error = exc
        if last_error:
            raise last_error
        raise OSError('no master addresses resolved')



def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError('non-finite JSON number')


def fetch_json(base_url, path, timeout, max_bytes, ca_file=None, bearer_token=None):
    """Bounded GET, explicit administrative path allowlist, no redirects/proxies."""
    parts = urllib.parse.urlsplit(base_url)
    if (parts.scheme not in ('http', 'https') or not parts.hostname
            or parts.username is not None or parts.password is not None
            or parts.path not in ('', '/') or parts.query or parts.fragment):
        raise ValueError('master_url must be an HTTP(S) origin without credentials')
    if path not in ('/vol/status', '/cluster/status'):
        raise ValueError('HTTP path is not allowlisted')
    if bearer_token and parts.scheme != 'https':
        raise ValueError('bearer credentials require HTTPS')
    context = ssl.create_default_context(cafile=ca_file) if parts.scheme == 'https' else None
    headers = {'Accept': 'application/json', 'User-Agent': 'db3-seaweedfs-inventory/1',
               'Connection': 'close'}
    if bearer_token:
        headers['Authorization'] = 'Bearer ' + bearer_token
    deadline = time.monotonic() + timeout
    port = parts.port or (443 if context else 80)
    connection = _DeadlineConnection(parts.hostname, port, deadline, context)
    try:
        connection.request('GET', path, headers=headers)
        with connection.getresponse() as response:
            if response.status != 200:
                raise ValueError('HTTP status ' + str(response.status))
            chunks, length = [], 0
            while True:
                _remaining(deadline)
                chunk = response.read1(min(65536, max_bytes + 1 - length))
                if not chunk:
                    break
                chunks.append(chunk)
                length += len(chunk)
                if length > max_bytes:
                    raise ValueError('response byte limit exceeded')
    finally:
        connection.close()
    return json.loads(b''.join(chunks).decode('utf-8'),
                      object_pairs_hook=_unique_object, parse_constant=_reject_constant)


def _map(value):
    if not isinstance(value, dict):
        raise ValueError('expected object')
    return value


def _text(value):
    if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 32 for c in value):
        raise ValueError('invalid string')
    return value


def _uint(value, maximum=2**32 - 1):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError('invalid unsigned integer')
    return value


def _bool(value):
    if type(value) is not bool:
        raise ValueError('invalid boolean')
    return value


def parse_inventory(document, max_records=10000):
    """Validate the entire normal-volume map; never return a truncated snapshot."""
    rows = {}
    try:
        version = _text(_map(document)['Version'])
        centers = _map(_map(document['Volumes'])['DataCenters'])
        seen = 0
        for dc, racks in centers.items():
            _text(dc)
            for rack, nodes in _map(racks).items():
                _text(rack)
                for node, volumes in _map(nodes).items():
                    _text(node)
                    if not isinstance(volumes, list):
                        raise ValueError('expected volume array')
                    for v in volumes:
                        seen += 1
                        if seen > max_records:
                            raise ValueError('record limit exceeded')
                        v = _map(v)
                        policy = _map(v['ReplicaPlacement'])
                        if set(policy) - {'dc', 'rack', 'node'}:
                            raise ValueError('unknown replication key')
                        row = {
                            'volume_id': _uint(v['Id']), 'disk_id': _uint(v['DiskId']),
                            'collection': _text(v['Collection']), 'data_center': dc,
                            'rack': rack, 'node_id': node,
                            'replication': ''.join(str(_uint(policy.get(k, 0), 9)) for k in ('dc', 'rack', 'node')),
                            'disk_type': _text(v['DiskType']),
                            'compact_revision': _uint(v['CompactRevision']),
                            'remote_storage_name': _text(v['RemoteStorageName']),
                            'read_only': _bool(v['ReadOnly']),
                            'read_only_can_delete': _bool(v['ReadOnlyCanDelete']),
                            'format_version': _uint(v['Version']),
                            'source_version': version,
                        }
                        key = (dc, rack, node, row['volume_id'], row['disk_id'])
                        if key in rows and rows[key] != row:
                            raise ValueError('conflicting duplicate replica')
                        rows[key] = row
    except (KeyError, TypeError) as exc:
        raise ValueError('missing or invalid inventory schema') from exc
    return [rows[k] for k in sorted(rows)]


def read_config(args):
    """Reject configuration errors before issuing any request or write."""
    args = _map(args)
    allowed = {'cluster', 'master_url', 'timeout_seconds', 'max_response_bytes',
               'max_records', 'ca_file', 'bearer_token_env'}
    if set(args) - allowed:
        raise ValueError('unknown trigger argument')
    try:
        cluster = _text(args['cluster'])
        url = _text(args['master_url'])
        timeout = float(args.get('timeout_seconds', '5'))
        max_bytes = int(args.get('max_response_bytes', '8388608'))
        max_records = int(args.get('max_records', '10000'))
    except (KeyError, TypeError, OverflowError) as exc:
        raise ValueError('missing or invalid configuration') from exc
    if (not cluster or cluster.endswith('\\') or not math.isfinite(timeout)
            or not 0.1 <= timeout <= 30
            or not 1024 <= max_bytes <= 52428800
            or not 1 <= max_records <= 100000):
        raise ValueError('configuration outside allowed limits')
    parts = urllib.parse.urlsplit(url)
    if (parts.scheme not in ('http', 'https') or not parts.hostname
            or parts.username is not None or parts.password is not None
            or parts.path not in ('', '/') or parts.query or parts.fragment):
        raise ValueError('master_url must be an HTTP(S) origin without credentials')
    # Accessing port validates malformed/out-of-range port strings up front.
    parts.port
    token = None
    if args.get('bearer_token_env'):
        token = os.environ.get(_text(args['bearer_token_env']))
        if not token or parts.scheme != 'https':
            raise ValueError('bearer credential requires populated environment variable and HTTPS')
    return cluster, url, timeout, max_bytes, max_records, args.get('ca_file'), token


def process_scheduled_call(influxdb3_local, call_time,
                           args: dict[str, str] | None = None) -> None:
    """Poll one master; buffer validated records followed by a snapshot manifest.

    A manifest is not a distributed commit; readers must reconcile its row count.

    Args:
        influxdb3_local: Engine context with logging and buffered-write methods.
        call_time: Unused schedule time; some builds supply naive local time.
        args: Required cluster and master_url plus optional collection settings.

    Raises:
        ValueError: Configuration is missing or invalid. Check the README table.
    """
    cluster, url, timeout, max_bytes, max_records, ca_file, token = read_config(args)
    timestamp = time.time_ns()
    snapshot_id = uuid.uuid4().hex
    try:
        status = _map(fetch_json(url, '/cluster/status', timeout, max_bytes, ca_file, token))
        leader = _text(status.get('Leader', ''))
        if not leader:
            raise ValueError('master has no known leader')
        document = fetch_json(url, '/vol/status', timeout, max_bytes, ca_file, token)
        rows = parse_inventory(document, max_records)
        lines = []
        for slot, row in enumerate(rows):
            line = (LineBuilder('seaweedfs_volume_replica_inventory')
                    .tag('cluster', cluster).tag('slot', str(slot))
                    .string_field('snapshot_id', snapshot_id))
            for key, value in row.items():
                if type(value) is bool:
                    line.bool_field(key, value)
                elif type(value) is int:
                    line.uint64_field(key, value)
                else:
                    line.string_field(key, value)
            line.time_ns(timestamp)
            # Exercise SDK validation for every record before buffering any.
            line.build()
            lines.append(line)
        marker = (LineBuilder('seaweedfs_inventory_poll').tag('cluster', cluster)
                  .string_field('snapshot_id', snapshot_id)
                  .bool_field('success', True).uint64_field('record_count', len(rows))
                  .string_field('source_version', _text(document['Version']))
                  .time_ns(timestamp))
        marker.build()
        for line in lines:
            influxdb3_local.write(line)
        influxdb3_local.write(marker)
        influxdb3_local.info('SeaweedFS inventory snapshot buffered; replicas=' + str(len(rows)))
    except Exception as exc:
        # Deliberately avoid exception messages, URLs, headers and raw topology.
        error_type = type(exc).__name__
        influxdb3_local.warn('SeaweedFS inventory collection failed: ' + error_type)
        failure = (LineBuilder('seaweedfs_inventory_poll').tag('cluster', cluster)
                   .string_field('snapshot_id', snapshot_id).bool_field('success', False)
                   .string_field('error_type', error_type).time_ns(timestamp))
        influxdb3_local.write(failure)
        # DB3 discards buffered writes when Python raises. Return normally so
        # the failure marker persists; consumers MUST inspect success, not just
        # the engine execution status. A failure to buffer this marker propagates.
        return None
