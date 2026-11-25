"""Scheduled contract tests with a recording API double, not a DB3 runtime test."""
import json
import unittest
from unittest.mock import patch
from test_inventory import ROOT, plugin

from line_builder_double import LineBuilder


class Context:
    def __init__(self):
        self.writes = []
        self.logs = []
    def write(self, builder):
        self.writes.append(builder.build())
    def info(self, message):
        self.logs.append(message)
    def warn(self, message):
        self.logs.append(message)


class ScheduledTests(unittest.TestCase):
    def setUp(self):
        self.mod = plugin()
        self.mod.LineBuilder = LineBuilder
        self.context = Context()
        self.document = json.loads((ROOT / 'tests/fixtures/volume_status.json').read_text())
        self.args = {'cluster': 'lab', 'master_url': 'http://master:9333'}
    def test_emits_one_consistent_snapshot_with_bounded_slot_tags(self):
        def fetch(url, path, *args, **kwargs):
            return {'Leader': 'opaque-leader'} if path == '/cluster/status' else self.document
        with patch.object(self.mod, 'fetch_json', side_effect=fetch):
            self.mod.process_scheduled_call(self.context, None, self.args)
        self.assertEqual(len(self.context.writes), 8)
        timestamps = {line.rsplit(' ', 1)[1] for line in self.context.writes}
        self.assertEqual(len(timestamps), 1)
        inventory = self.context.writes[:7]
        self.assertTrue(all(line.startswith('seaweedfs_volume_replica_inventory,cluster=lab,slot=') for line in inventory))
        self.assertIn('success=t', self.context.writes[-1])
        self.assertIn('record_count=7u', self.context.writes[-1])
        self.assertTrue(all('snapshot_id=' in line for line in self.context.writes))
    def test_failed_fetch_emits_only_failure_without_secret_or_topology_log(self):
        with patch.object(self.mod, 'fetch_json', side_effect=TimeoutError('secret host info')):
            self.mod.process_scheduled_call(self.context, None, self.args)
        self.assertEqual(len(self.context.writes), 1)
        self.assertIn('success=f', self.context.writes[0])
        self.assertNotIn('record_count=', self.context.writes[0])
        self.assertNotIn('secret', repr(self.context.logs))
    def test_unknown_leader_suppresses_inventory(self):
        with patch.object(self.mod, 'fetch_json', return_value={}):
            self.mod.process_scheduled_call(self.context, None, self.args)
        self.assertFalse(any(line.startswith('seaweedfs_volume_replica_inventory') for line in self.context.writes))
    def test_invalid_config_rejected_before_network_or_writes(self):
        for args in [None, {}, {'cluster': 'x'}, {**self.args, 'timeout_seconds': 'NaN'}, {**self.args, 'max_records': '0'}, {**self.args, 'typo': 'yes'}]:
            with self.subTest(args=args), patch.object(self.mod, 'fetch_json') as fetch:
                with self.assertRaises(ValueError):
                    self.mod.process_scheduled_call(self.context, None, args)
                fetch.assert_not_called()
        self.assertEqual(self.context.writes, [])
    def test_write_failure_cannot_emit_success_marker(self):
        class FailingContext(Context):
            def write(self, builder):
                if builder.measurement == 'seaweedfs_volume_replica_inventory':
                    raise RuntimeError('write failed')
                super().write(builder)
        context = FailingContext()
        with patch.object(self.mod, 'fetch_json', side_effect=[{'Leader': 'master'}, self.document]):
            self.mod.process_scheduled_call(context, None, self.args)
        self.assertFalse(any('success=t' in line for line in context.writes))
