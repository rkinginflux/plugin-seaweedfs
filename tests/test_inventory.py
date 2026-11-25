"""Behavior tests; synthetic topologies are explicit test data."""
import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


def plugin():
    path = ROOT / 'seaweedfs_inventory.py'
    if not path.exists():
        raise AssertionError('SeaweedFS inventory plugin is not implemented')
    spec = importlib.util.spec_from_file_location('seaweedfs_inventory', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class InventoryTests(unittest.TestCase):
    def test_explorer_metadata_declares_scheduled_arguments(self):
        metadata = json.loads(plugin().__doc__)
        self.assertEqual(metadata['plugin_type'], ['scheduled'])
        required = {a['name'] for a in metadata['scheduled_args_config'] if a['required']}
        self.assertEqual(required, {'cluster', 'master_url'})

    def test_455_shaped_fixture_preserves_replica_identity_and_policy(self):
        mod = plugin()
        data = json.loads((ROOT / 'tests/fixtures/volume_status.json').read_text())
        rows = mod.parse_inventory(data, max_records=100)
        self.assertEqual(len(rows), 7)
        self.assertEqual({r['volume_id'] for r in rows}, {1, 2, 3, 5, 6, 8, 9})
        row = rows[0]
        self.assertEqual(row['replication'], '000')
        self.assertEqual(row['data_center'], 'DefaultDataCenter')
        self.assertEqual(row['rack'], 'DefaultRack')
        self.assertEqual(row['node_id'], 'volume-a.example.test:8080')
        self.assertNotIn('size', row)
        self.assertNotIn('file_count', row)
        self.assertNotIn('under_replicated', row)


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.mod = plugin()
        self.doc = json.loads((ROOT / 'tests/fixtures/volume_status.json').read_text())
        dc = self.doc['Volumes']['DataCenters']['DefaultDataCenter']
        self.volumes = dc['DefaultRack']['volume-a.example.test:8080']

    def test_nonzero_replication_is_canonical_and_node_is_opaque(self):
        self.volumes[0]['ReplicaPlacement'] = {'dc': 1, 'rack': 2, 'node': 3}
        rows = self.mod.parse_inventory(self.doc)
        self.assertEqual(next(r for r in rows if r['volume_id'] == self.volumes[0]['Id'])['replication'], '123')

    def test_missing_or_null_policy_is_not_zero(self):
        for value in (None, '001', {'dc': -1}, {'rack': True}, {'node': 10}):
            with self.subTest(value=value):
                self.volumes[0]['ReplicaPlacement'] = value
                with self.assertRaises(ValueError):
                    self.mod.parse_inventory(self.doc)
        self.volumes[0].pop('ReplicaPlacement')
        with self.assertRaises(ValueError):
            self.mod.parse_inventory(self.doc)

    def test_bad_map_null_list_and_non_integer_ids_fail_closed(self):
        for value in (None, [], {'x': None}):
            self.doc['Volumes']['DataCenters'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.mod.parse_inventory(self.doc)
        self.doc['Volumes']['DataCenters'] = {}
        self.assertEqual(self.mod.parse_inventory(self.doc), [])

    def test_record_cap_never_silently_truncates(self):
        with self.assertRaises(ValueError):
            self.mod.parse_inventory(self.doc, max_records=6)

    def test_exact_duplicates_deduplicate_conflicts_fail(self):
        self.volumes.append(dict(self.volumes[0]))
        self.assertEqual(len(self.mod.parse_inventory(self.doc)), 7)
        self.volumes[-1]['CompactRevision'] += 1
        with self.assertRaises(ValueError):
            self.mod.parse_inventory(self.doc)

    def test_negative_boolean_and_oversized_integer_rejected(self):
        for value in (-1, True, 2**64):
            self.volumes[0]['Id'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.mod.parse_inventory(self.doc)


if __name__ == '__main__':
    unittest.main()
