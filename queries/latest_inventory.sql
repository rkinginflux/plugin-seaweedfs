-- Latest count-reconciled successful snapshot per cluster, with freshness bound.
-- slot is a bounded row discriminator, NEVER a persistent replica identity.
-- An empty latest snapshot returns zero rows; inspect latest_poll.sql as well.
WITH counts AS (
    SELECT cluster, time, snapshot_id, COUNT(*) AS observed_count
    FROM seaweedfs_volume_replica_inventory
    WHERE time > now() - INTERVAL '10 minutes'
    GROUP BY cluster, time, snapshot_id
), reconciled AS (
    SELECT p.cluster, p.time, p.snapshot_id, p.record_count,
           ROW_NUMBER() OVER (PARTITION BY p.cluster ORDER BY p.time DESC) AS rn
    FROM seaweedfs_inventory_poll p
    LEFT JOIN counts c ON p.cluster = c.cluster AND p.time = c.time
                      AND p.snapshot_id = c.snapshot_id
    WHERE p.time > now() - INTERVAL '10 minutes' AND p.success = true
      AND p.record_count = COALESCE(c.observed_count, 0)
)
SELECT i.time, i.cluster, i.volume_id, i.collection, i.data_center, i.rack,
       i.node_id, i.disk_id, i.replication, i.compact_revision,
       i.disk_type, i.remote_storage_name, i.read_only, i.read_only_can_delete,
       i.format_version, i.source_version, i.snapshot_id
FROM seaweedfs_volume_replica_inventory i
JOIN reconciled s ON i.cluster = s.cluster AND i.time = s.time
                 AND i.snapshot_id = s.snapshot_id
WHERE s.rn = 1
ORDER BY i.cluster, i.volume_id, i.data_center, i.rack, i.node_id, i.disk_id;
