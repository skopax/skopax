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
- With pg_stat_statements behind QAN only sums and counts are real: per-call max and p99 read 0. A true per-call max needs pg_stat_monitor; the closest stand-in is the worst minute's average.
- One period_start holds several rows per query (one per user or client): sum per minute first, then aggregate.
- The baseline can be the broken number: a query that follows business hours or a daily batch is far cheaper at the weekend, so a 1-day baseline on a Monday makes every normal minute look like a spike. Take the larger of 1 d, 7 d and the window's own average.
- Top queries by DB time in the last hour: SELECT database, substring(fingerprint,1,80) AS q, sum(num_queries) AS calls, round(sum(m_query_time_sum),1) AS total_s, round(1000*sum(m_query_time_sum)/sum(num_queries),1) AS avg_ms FROM pmm.metrics WHERE service_name='<server>' AND period_start > now() - INTERVAL 1 HOUR GROUP BY database, fingerprint ORDER BY total_s DESC LIMIT 10.
- Worst minutes of one query: the same with queryid='<id>', GROUP BY period_start, ORDER BY avg_ms DESC LIMIT 3; leave out the monitoring user.

How the checks compare:
- Every window is 60 minutes; the same window one day and one week earlier is the baseline.
- A Postgres or MongoDB service that answered in a baseline window but not now is "not reported by PMM": its exporter or pmm-agent stopped, or the server is gone.
- A `no-access` card means PMM refused a read the check needs: the part it names (data sources, metrics, QAN) is not read, and the cards of that check are neither opened nor closed until the token gets the role the card names.
- Instance options: split-users are database users read apart from the application (their connection jumps are `pg-conn-split` info cards and their queries are info cards keyed `...|split`, left out of the application cards); qan-app turns a database name into the application name on a Query Analytics card; link-base is the address the card links open instead of the PMM URL.

Reading connection signatures:
- Who grew: sum by (usename) or (datname) of pg_stat_activity_count at a time before and one inside the incident. Who moved first: instant answers (`at`) a few seconds apart across the jump minute.
- sum(pg_stat_activity_count) flat at max_connections minus the reserved slots = the server is full and refuses new connections, so pools in every service wait and time out.
- Server connections up while the apps' pool connections (sum(hikaricp_connections), from the apps' Prometheus when one is connected) go down = the extra connections are not live app pools.
- Every user roughly doubling within the same few seconds = a reconnect storm (every client lost its connections at once), not one misbehaving service.
- Apps logging failed connection validation while the DB host still counts the sockets as established and sends no resets = the connections were cut between app and DB (a proxy, VIP or firewall). A sharp drop exactly some minutes later with no postmaster restart means something in the middle timed the orphans out.
- Restart or not: pg_postmaster_start_time_seconds unchanged = no restart. When liveness probes do not include the database, DB trouble only takes pods out of readiness, so restarts at the same time have another cause.
- A delayed standby shows a constant replication lag equal to its delay: that is its setting, not a stall.

Lock stalls, in this order:
1. Per-minute QAN buckets of the query: sum num_queries and m_query_time_sum per period_start in an inner query, then per hour take the worst minute's average and its call count. A query stall card is one such minute far above the usual average; it reads as far fewer calls at a much higher average, which points to waiting (a lock or I/O) rather than more load. When one minute's DB time (average x calls) is above 60 s, more than one session was inside the statement, so someone waited.
2. Raw lock waits: pg_lock_conflicts_count comes from a collector query that not every PMM has, and it is present only while a session waits, so its absence cannot tell a missing collector from a quiet server. It is sampled sparsely, so wrap it before a subquery: max_over_time(sum by (service_name) (last_over_time(pg_lock_conflicts_count[1m]))[60m:1m]) and count_over_time((sum by (service_name) (last_over_time(pg_lock_conflicts_count[1m])) > 0)[60m:1m]). Without last_over_time the subquery answers nothing or a fraction of the samples. It names no query: take the blocked statement from qan or the app logs.
3. Reading it: a steady floor of waiters hour after hour is one long-running writer, not an incident; tie it to a job's start and finish lines before you call anything new.
4. The value is not a headcount: a chain is counted once per pid ahead of it. Cross-check a spike with sum by (datname) (pg_stat_activity_count{state="active"}) at a few times across the minute, which also names the database. Held locks alone decide nothing; RowShare with RowExclusive pairs are the SELECT ... FOR UPDATE signature.
