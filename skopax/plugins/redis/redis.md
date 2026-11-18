`redis` reads Redis health through a Prometheus instance: the redis_exporter series of each Redis instance, and the Lettuce client timings of each service that calls Redis.

- Cards: `redis-down` (the exporter cannot reach Redis), `redis-master` (no instance of a namespace reports the master role), `redis-repl` (the masters of a namespace with fewer connected replicas in total than its replica instances), `redis-evict` and `redis-reject` (evicted keys or refused connections in 30 minutes), `redis-app` (a service's Redis calls slower than 1 and 7 days earlier) and `redis-app-err` (failed Redis calls of a service).
- Order: `op instances` for up, role, replicas, memory and clients; then `op apps` with the card's service as `job` to compare its call time with the same window 1 and 7 days earlier.
- A `redis-app` card on one service while Redis looks quiet (steady ops/s, no evictions or rejected connections) points at the service side: its connection pool, the network, or large values. Slow calls on many services at once point at Redis itself: slow commands such as KEYS or large scans run on one thread and queue everything behind them.
- Evictions mean memory is full and Redis drops keys by its eviction policy; rejected connections mean the client limit was hit, so pods that start or scale out cannot connect.
- Master and replica cards come only from series that carry the `role` label, grouped by namespace.
- For anything else use `promql` on the same Prometheus instance (`redis_*` and `lettuce_seconds_*` series).
