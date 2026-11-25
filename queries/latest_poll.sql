-- Always inspect timestamp/success; a stale last success is not current health.
SELECT * FROM (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY cluster ORDER BY time DESC) AS rn
    FROM seaweedfs_inventory_poll
    WHERE time > now() - INTERVAL '1 hour'
) WHERE rn = 1;
