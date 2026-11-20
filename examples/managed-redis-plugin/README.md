# Managed Redis, an example Skopax plug-in

Managed Redis reads Redis instances that you cannot reach directly, such as a managed cloud Redis, whose metrics reach you only through a Grafana datasource. It opens cards for a Redis that is down, near its memory or client limit, slower than a day or a week ago, evicting keys, rejecting connections or just restarted, and gives the investigator a tool to read the instances and their history. Like the [Couchbase example](../couchbase-plugin/README.md), it builds on [Hello](../hello-plugin/README.md); read Hello first.

The `managed-redis` preset of [custom checks](../../docs/custom-checks.md#presets) now covers the common case without a plug-in; this example stays as an example of a full plug-in.

## When to use it instead of the built-in Redis integration

Skopax's built-in `redis` integration reads `redis_exporter` and Lettuce series from a Prometheus instance that Skopax can query itself. Use it whenever you can. This example is for the other case: the series sit behind Grafana, in a datasource that Skopax cannot add as a Prometheus instance of its own (for example a cloud provider's metrics service that only Grafana is allowed to query), and the Redis servers cannot be reached either. The plug-in then queries the datasource through Grafana's datasource proxy, with the Grafana instance's own token.

## What is in it

| File | What it shows |
| --- | --- |
| `pyproject.toml` | The package, and the `skopax.plugins` entry point named `managed-redis` that points at `skopax_managed_redis:PLUGIN`. |
| `skopax_managed_redis/__init__.py` | The `Plugin`: four fields, a Test connection, one check with a rule set, card types, card links and one read-only tool. It has no page. |
| `skopax_managed_redis/icon.svg` | The icon on the Integrations tile. |
| `tests/test_managed_redis.py` | Unit tests that run the check, Test connection and the tool with a fake Grafana. |
| `Dockerfile` | A layer image: the Skopax image with this plug-in added. |
| `LICENSE` | The Apache License 2.0 text, which the wheel carries (`license-files`). |

### The plug-in, beyond Hello

- **A Grafana peer.** `uses=('grafana',)` and `Field('grafana', 'Grafana', 'instance', choices=('grafana',))` let the admin pick a Grafana instance. The check reads its `url` and `token` from `ctx.peer('grafana').info.config` and sends every request through the peer's transport, `peer.http()`, which carries that instance's proxy and CA settings. It reads through `skopax.kit.grafana.Datasource`, which looks the datasource up once by name (`GET /api/datasources`) and then queries it through `/api/datasources/proxy/uid/<uid>/api/v1/query`.
- **Fields with defaults from `redis_exporter`.** `datasource` names the Grafana datasource. `prefix` (default `redis_`) and `label` (default `instance`) say how the series are named: the community `redis_exporter` writes `redis_up`, `redis_connected_clients`, `redis_config_maxclients`, `redis_memory_used_bytes`, `redis_memory_max_bytes`, `redis_evicted_keys_total`, `redis_rejected_connections_total`, `redis_uptime_in_seconds`, `redis_commands_processed_total` and `redis_commands_duration_seconds_total`. A setup that renames them, such as `cloud_redis_up` with a `name` label, changes the two fields and nothing else.
- **Checking a field.** Both values are pasted into PromQL, so the pass, Test connection and the tool refuse a prefix that is not the start of a metric name and a label that is not a label name, before any request. Values from series labels and tool arguments go into queries through `json.dumps`, so a quote in an instance name cannot change a query.
- **Baselines with `offset`.** The average command time is compared with the same 30 minutes 1 and 7 days earlier, read with `offset 1d` and `offset 7d` in the query itself, where the Couchbase example asks three times. Without a baseline there is no command-time card.
- **Rules and limits.** `RuleSet('rules', 'Managed Redis thresholds', RULES, RULE_HELP)` holds the memory, client, command-time and uptime thresholds. The memory rule needs `redis_memory_max_bytes`: when no limit is set it is 0, and the rule is skipped rather than reading every Redis as full.
- **Card types and keys.** Seven kinds, each starting with `managed-redis-`: down, memory, clients, latency, evicted, rejected and restart. A down Redis gets only the down card. The keys are `managed-redis|<instance>|<redis>|<kind>`, so two Skopax instances of the plug-in never share a card.
- **Links.** Each card links to Grafana Explore with its own query on the same datasource, and only when the Grafana address is `https://`.
- **No access.** A 401 or 403 from Grafana names the right the token lacks: `datasources:read` for the datasource list, `datasources:query` for a query. The pass raises one access card through `kit.access`, ends `ok` with the tail `no access to Redis metrics` and keeps the other cards open.
- **Errors keep the cards.** No peer, a datasource that does not exist, a Grafana error, or a datasource with no `up` series fails the pass, and a failed pass clears no card. A wrong prefix therefore shows as an error rather than as every Redis being fine.
- **A tool with `scan=True`.** `Tool('managed_redis', ...)` reads live: `op instances` lists every instance with its numbers, down ones first and capped at 50 rows with a note that says so, and `op history` returns one metric of one instance over up to 48 hours, at most 120 points. `scan=True` makes Skopax mask secrets in the answer before the model sees it, `per_run=6` caps the calls, and a refused read returns the same sentence as the access card.
- **No page.** The check still writes its `managed-redis` section, which the investigator's pass table reads, but declares no `Panel`. A plug-in needs no page to be useful.

## Run the tests

From the root of the Skopax repository:

```sh
.venv/bin/python -m pytest -q -n 0 examples/managed-redis-plugin/tests
```

## Build the layer image

```sh
docker build --build-arg SKOPAX_VERSION=0.4.4 -t skopax-managed-redis examples/managed-redis-plugin
```

Pin `SKOPAX_VERSION` to the Skopax release you run. Managed Redis is then listed in Connect > Integrations; add the Grafana instance first, with a token that may read and query datasources.

## Licence

Apache-2.0, like Skopax (see `LICENSE`). Copyright 2026 Maksut Spahi.
