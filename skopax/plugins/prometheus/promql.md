Use the promql tool for questions the cards and tables cannot answer. It runs PromQL against one Prometheus instance. When several instances are configured, pass the cluster input to pick one; without it the primary instance answers.

What the scheduled checks measure, so you can reproduce a card:
- Log volume: `log4j2_events_total` by `level` ("error", "warn") per `namespace` and `job`.
- Responses: `http_server_requests_seconds_count` with `status=~"5.."` for 5xx, and the `_sum` over the `_count` for average latency.
- Outbound calls: `http_client_requests_seconds_count` with `outcome="SERVER_ERROR"`, or `outcome="UNKNOWN"` with an exception.
- Gateway and ingress: `krakend_proxy_duration_count` and `nginx_ingress_controller_requests`.
- Pools and queues: `hikaricp_connections_timeout_total`, `kafka_consumergroup_lag_sum`.
- Pods and nodes: `kube_pod_container_status_restarts_total`, `kube_deployment_status_replicas_unavailable`, `kube_node_status_condition`.

How the checks compare:
- Every window is 60 minutes. The same window is read one day and one week earlier as the baseline.
- Counters are compared with `increase(metric[60m])`, which gives the total added in the window. Use `rate(metric[5m])` only to see a per-second shape inside the window; do not compare a rate with a card's count.
- A rate card opens when the current count is more than twice the baseline and above the baseline plus a floor set in the rate floors rule set.

Query tips:
- Select a service with `namespace="shop",job="checkout-service"`.
- Use an instant query for one number and give range_minutes for a trend of about 200 points.
- Times are unix seconds in UTC; leave `at` empty for now.
