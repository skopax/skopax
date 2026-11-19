Use pmm_query and qan for questions about Postgres and MongoDB that the cards and the Postgres and MongoDB pages cannot answer. Both read one PMM server; when several are configured, pass the instance input to pick one, otherwise the primary answers.

pmm_query runs PromQL on PMM's metrics data source. What the scheduled checks read:
- Postgres: `pg_up`, `pg_settings_max_connections`, `pg_stat_activity_count` (by `state`, `usename`, `datname`), `pg_stat_activity_max_tx_duration`, `pg_replication_lag_seconds` with `pg_replication_is_replica`, `pg_stat_database_deadlocks`, `pg_lock_conflicts_count`.
- MongoDB: `mongodb_up`, `mongodb_mongod_replset_my_state` (1 primary, 2 secondary, 7 arbiter), `mongodb_connections` (`state="current"` and `"available"`), `mongodb_mongod_replset_member_replication_lag`, `mongodb_op_counters_total`, `mongodb_mongod_op_latencies_latency_total` over `_ops_total` (microseconds per operation), `mongodb_mongod_global_lock_current_queue`.
- Select a server with `service_name="..."`; PMM names every series by its service.

qan runs one ClickHouse SELECT on PMM Query Analytics, table `pmm.metrics` only. The table holds every service type PMM watches; add `service_type = 'postgresql'` (or `'mongodb'`, `'mysql'`) to stay on one. Useful columns:
- `service_name`, `database`, `username`, `queryid`, `fingerprint` (the normalised query text), `period_start` (one row per minute bucket);
- `num_queries`, `m_query_time_sum` (seconds), `m_rows_sent_sum`.
- The time bound is the `hours` input; still add `period_start > now() - INTERVAL 1 HOUR` or similar, and aggregate with `sum` and `GROUP BY queryid`.
- An average per call is `1000 * sum(m_query_time_sum) / sum(num_queries)` in milliseconds.

How the checks compare:
- Every window is 60 minutes; the same window one day and one week earlier is the baseline.
- A Postgres or MongoDB service that answered in a baseline window but not now is "not reported by PMM": its exporter or pmm-agent stopped, or the server is gone.
- Lock waits come from `pg_lock_conflicts_count`, a collector query that not every PMM has. It is present only while a session waits, so its absence cannot tell a missing collector from a quiet server. It is sampled at its collector's resolution and names no query, so take the blocked statement from qan or the app logs.
- A query stall is one minute bucket whose average is far above the usual average, which points to waiting (a lock or I/O) rather than more load.
- A `no-access` card means PMM refused a read the check needs: the part it names (data sources, metrics, QAN) is not read, and the cards of that check are neither opened nor closed until the token gets the role the card names.
- Instance options: split-users are database users read apart from the application (their connection jumps are `pg-conn-split` info cards and their queries are info cards keyed `...|split`, left out of the application cards); qan-app turns a database name into the application name on a Query Analytics card; link-base is the address the card links open instead of the PMM URL.
