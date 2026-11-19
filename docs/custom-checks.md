# Custom checks

A custom check turns a PromQL query into cards without a plug-in. An admin defines it in Admin > Integrations, under the integration tiles, and it reads through a Prometheus instance or through a Prometheus datasource of a Grafana instance that is already set up there. A check is plain data (JSON), so a ready-made set of checks can be shared as data too.

Only admins list, add, change, preview or delete checks. The cards they make are ordinary cards: they are pushed, assigned, muted and resolved like any other.

## A check

| Field | What it holds |
| --- | --- |
| `id` | 2 to 41 lowercase letters, digits or dashes, starting with a letter. Made from the name when it is left out (a name that starts with a digit gets `check-` in front; a name with too few letters or digits asks for an id). It never changes, so a rename keeps the open cards. |
| `name` | 1 to 80 characters. Every card of the check carries it in the place of a service name, and Slack shows it as `<name> / <title>`. |
| `enabled` | `true` (the default) or `false`. A check that is off runs no query and its cards clear. |
| `source` | `prometheus` or `grafana`. |
| `instance` | The name of the Prometheus or Grafana instance. |
| `datasource` | For `grafana` only: the name or the uid of the datasource. A name is looked up in `GET /api/datasources` once a pass per Grafana instance; when a name is shared, the Prometheus-type datasource wins. A uid keeps working after the datasource is renamed. |
| `query` | An instant PromQL query, at most 2000 characters. It must give an instant vector or a scalar. |
| `op` and `threshold` | The comparison, one of `>`, `>=`, `<`, `<=`, `==` and `!=`, and a finite number. |
| `baselines` and `baselineMode` | Optional: `1d` and `1w`, the same query at the same time 1 day or 1 week earlier, compared as a `ratio` (now / then, the default) or a `difference` (now - then). |
| `severity` | `info`, `warn` (the default) or `crit`. |
| `by` | At most 5 label names. One card per value set of these labels; empty means one card for the whole check. |
| `for` | 1 to 12: how many passes in a row a label set must match before its card opens. |
| `title` and `text` | The card title (at most 200 characters) and text (at most 1000), as plain text with placeholders. |

Placeholders are `{value}` and `{<label>}` for any label of the series, such as `{instance}`. Nothing else is allowed in braces: no formats, attributes, indexes or expressions, and a stray brace is refused when the check is saved. A label the series lacks is filled with nothing. Each label value is cut to 200 characters and folded to one line, and a value that holds a placeholder is not filled again. In a label value, a `*` becomes U+2217 (a math asterisk) and a backtick an apostrophe, so a label value cannot bold text or open code in Slack; the card on the page shows them the same way; `<`, `>` and `&` are escaped on the page and in Slack, so `<!channel>` or a tag stays text. An empty title is the check name followed by the `by` label values. The text is followed by the comparison, such as `Redis memory: 93.5 > 90.`

## How a check runs

The core pass `custom-checks` runs every 5 minutes, while at least one check or one of its cards exists, and soon after a check is added, changed or deleted. It runs each check that is on, the one that waited longest first:

1. It runs the query at the pass time. Series whose value is NaN, +Inf or -Inf, or not a number, are left out and counted as not a number.
2. It groups the series by the `by` labels. When a group holds several series, the one with the largest value counts for `>` and `>=`, the smallest for `<` and `<=`, and the first for `==` and `!=`.
3. With baselines, it runs the same query again with the time set 1 day and 1 week earlier; the query itself is not changed, so `a / b` works as it is. A group fires only when the comparison holds against every chosen baseline. A group with no baseline value, or with a ratio to a baseline of 0, does not fire.
4. A group gets a card once it has fired for `for` passes in a row, counted by time: it must have fired since `(for - 1) x 5` minutes ago, give or take half a pass, so an extra pass soon after a change does not count. A pass where it does not fire, or where the check fails, starts its count again, so after an outage a card waits the full `for` once more. A group whose card is open keeps it while it fires, also right after an edit of the check.

The card key is `custom-checks|<id>|<hash of the by values>`, and its ran key is `custom-checks:<id>`. The card value is the group's value, and with baselines the card also shows the 1 day and 1 week values. A link opens the query in Prometheus (when its UI URL is `https`) or in Grafana Explore.

An empty result is a quiet pass: no card and no failure. Groups with no baseline value (a new series, or one older than the retention) are counted: the Admin row and Preview say `N groups had no baseline value`.

## Bounds

- **Time:** the queries of one check, baselines included, have about 20 s together (a query that starts just before the limit may run a little past it); each query is also sent with a `timeout` of 20 s. The whole pass has 240 s; a check that does not start in time shows `not run: the pass ran out of time` and runs first at the next pass. When a source times out or cannot be reached, the other checks on that instance show `not run: <source> <instance> did not answer earlier in this pass` instead of waiting their turn, so one slow source does not hold up the checks on other sources.
- **Series:** at most 500 series are read per check, sorted by their labels; the rest are counted but not read. An answer is at most 8 MiB.
- **Cards:** at most 20 cards per check. Past that, 19 label sets get a card (those with an open card first, then the largest values for `>` and `>=` and the smallest for `<` and `<=`) and one more card, `custom-checks|<id>|-|more`, counts and names the rest.
- **Checks:** at most 50; two admins adding at the same moment cannot pass the cap.

The same bounds hold for Preview.

## When a check fails

A bad query, an unreachable source, a refused read, a missing instance or datasource, or an answer that is not a query result fails that check only. Its row in Admin shows `failing` with the reason, such as `Prometheus main answered HTTP 400: 1:9: parse error: unexpected <EOF>`, `Grafana datasources: HTTP 403 (the token needs datasources:read)`, `the queries took longer than 20 s` or `no Grafana datasource named cloud-metrics`, and the pass status adds `1 of 3 custom checks failing: Redis memory` after `ok`. A failing check raises no card and closes none: its open cards stay as they were until it runs again, and like any card they close after a day unseen.

## Cards that close

- A label set that stops matching has its card cleared at the next pass, as any card is.
- When a label value changes, the old card clears and the new one opens.
- Turning a check off or deleting it clears its cards at once, also while a pass is running: that pass leaves its cards closed.
- When the query, source, comparison, threshold, baselines or `by` labels change while a pass is running, that pass makes no cards for the check and closes none; the next pass runs the new definition. A change of the name, texts or severity alone does not hold the pass back.
- A renamed check keeps its id, so its open cards stay open and take the new name and texts at the next pass.
- When a Grafana datasource named in a check is renamed, the check fails until its `datasource` names the new name or the uid.

## Preview

Preview in the editor runs the check once, with the values in the form and without saving, and shows the cards it would make: title, text, value and severity, how many series it read and how many match, and how many groups had no baseline value. It ignores `for` and says after how many passes a card would open. A failing check shows the reason. Preview runs the same code as the pass, with the same time limit and caps.

## The API

The page uses these routes; all of them need the admin role.

- `GET /api/admin/custom-checks` lists the checks, each with `run` (its last state: `ok`, `failing` or `off`, the reason, the series read, the cards made) and, for a check added from a preset, `preset` and `presetValues`; then the instances that can be a source, the limits, the choices and the presets (`id`, `name` and `nameKey`).
- `POST /api/admin/custom-checks` adds a check (201). A taken id or a full list is 409, a bad field 422 with the reason.
- `PUT /api/admin/custom-checks/<id>` changes a check; the id in the path wins.
- `DELETE /api/admin/custom-checks/<id>` deletes it and clears its cards (204).
- `POST /api/admin/custom-checks/preview` runs a check once without saving.
- `GET /api/admin/custom-checks/presets` lists the presets: `id`, `name`, `summary`, `source`, `needs`, the `placeholders` (`key`, `kind`, `label`, `help`, `example`, `default`, `optional`, `max`) and the `checks` (`key`, `name`, `severity`).
- `POST /api/admin/custom-checks/presets/<id>/render` takes `{"values": {...}}` and answers the `values` with the defaults filled in and the `checks` the preset would add, without saving; Preview sends each of them to `/preview`. A bad value is 422 with the reason, an unknown preset 404.
- `POST /api/admin/custom-checks/presets/<id>` takes the same body and adds the checks (201, `items`). The same preset with the same values, an id that is taken, or a list that cannot take them all is 409, and nothing is added. When a store error half-way leaves checks that cannot be taken back, the answer (500, or 409 for a lost cap race) and the log name them.

Every add, change and delete is a line in History; adding a preset is one line, such as `Preset Couchbase added for orders-prod: 15 custom checks`.

## Presets

A preset is a ready-made set of custom checks for one kind of system. It is data, not code: one JSON file per preset under `skopax/presets/`, checked when Skopax starts with the same rules as a check you write, and filled in only with values that are checked too. Under the custom checks, **Add from preset** lists the presets with what each watches and the source it needs. Choosing one asks for its placeholders; **Preview** fills them in and runs each check once, one after the other, as the editor's Preview does (Cancel or Back stops it); **Add** saves them. Escape closes the picker.

A layer image can ship more presets: add JSON files to `skopax/presets/` in the image, in the shape of the shipped ones. A file that breaks the rules stops Skopax from starting, with the file and the reason in the log, because a file built into an image should fail where it was built, not on an admin's click.

A placeholder has a kind, and a value must have the shape of its kind:

| Kind | Where it goes | A value |
| --- | --- | --- |
| `instance` | the check's `instance` | the Prometheus or Grafana instance, picked from the instances set up under Integrations |
| `datasource` | the check's `datasource` (Grafana presets) | the name or uid of the datasource |
| `name` | the check ids, names and texts | 2 to 20 lowercase letters, digits or dashes, such as `orders-prod`, so the same preset can be added once per cluster |
| `matchers` | inside the braces of every selector in the queries | label matchers, `label op "value"` joined by commas, such as `job=~".*couchbase.*",env="prod"`. Skopax checks the shape only: a label name, one of `=`, `!=`, `=~` and `!~`, and a double-quoted value with backslash escapes, so a value cannot close the braces, add `or` or reach other series; whether Prometheus accepts a regex shows at Preview. Some presets allow it empty, which reads every series |
| `prefix` | the start of the metric names | the start of a metric name, such as `redis_` |
| `label` | the label the queries group by, and the `by` labels | a label name, such as `instance`; not `value`, `base` or a name that starts with `__` |

An empty value takes the default where the preset has one. A value may be at most the preset's `max` characters long, chosen so that the longest value still fits every query in 2000 characters; a longer one is refused. A value is filled in once: a value that holds `${...}` stays as it is.

The checks of a preset are normal checks. Their ids are made from the preset, the name and the check, such as `cb-orders-prod-down`, and each carries `preset` (the preset id) and `presetValues` (the values filled in when it was added; an edit does not change them). The list groups them under a chip with the preset name and the name value. Each one can be changed, turned off or deleted like any check; a change keeps it in its group.

- **Twice.** The same preset with the same values (after the defaults) is refused, naming the checks that are already there, also when some of them were changed or deleted. A different name and selector add a second set, such as one per cluster. An id that another check already has refuses the whole preset; choose another name.
- **The cap.** A preset adds all its checks or none: when they do not all fit under the 50 checks, it is refused and nothing is added.
- **Removing a preset.** Delete its checks: Edit, then Delete, on each row of its group (or `DELETE /api/admin/custom-checks/<id>` for each). Their cards clear at once. Once every check of the set is gone, the preset can be added again with the same values.

A preset check behaves like the example plug-in it comes from, with these differences: when the selector matches nothing, the `no-series` check raises one critical card and the other checks are quiet, where the plug-in fails its pass; a 401 or 403 makes the checks fail where the plug-in raises one access card; a value that moves between warn and crit closes the card of one check and opens the card of the other (`memory` and `memory-crit`, `n1ql-errors` and `n1ql-errors-crit`), where the plug-in changes the severity of one card; there is no page and no investigator tool; and the texts give durations in seconds.

CI runs every preset check in `promtool test rules` on the test data of the example plug-ins (now, 1 day and 1 week earlier) and expects the cards the plug-ins give; `tests/presets_promtool.py` writes that test file.

### Couchbase

Reads a Couchbase cluster through a Prometheus instance that scrapes the Couchbase exporter, with the checks, thresholds and texts of the [Couchbase example plug-in](../examples/couchbase-plugin/README.md). Placeholders: `instance` (the Prometheus instance), `cluster` (a name such as `orders-prod`) and `selector` (the matchers that pick the cluster, `job=~".*couchbase.*"` by default, at most 100 characters). Per node means one card per `instance` label.

| Check | Severity | Fires when |
| --- | --- | --- |
| `no-series` | crit | the selector matches no `up` series |
| `down` | crit | a node's `up` is below 0.5 |
| `not-scraped` | warn | a node had an `up` series a day ago and has none now (quiet when the cluster has no `up` series at all; `no-series` covers that) |
| `restart` | warn | a node that is up has `kv_uptime_seconds` below 3600 |
| `conn-jump` | warn | a node's peak of `kv_curr_connections` in 60 min is more than 1.5 times, and 200 more than, the larger peak of the same window 1 day and 1 week earlier; no card without either |
| `index-memory` | info | a node uses at least 90 % of its index memory quota (skipped when the quota is 0) |
| `failover` | crit | `cm_auto_failover_count` rose in 60 min |
| `rebalance-stuck` | crit | `cm_rebalance_stuck` is set |
| `rebalance` | info | a rebalance is running and is not stuck |
| `n1ql-errors` and `n1ql-errors-crit` | warn below 5 %, crit from 5 % | at least 20 N1QL errors in 60 min and an error share above 1 % with no baseline, else above twice and 0.5 points above the larger share of 1 day and 1 week earlier |
| `n1ql-timeouts` | warn | at least 5 N1QL timeouts in 60 min, and more than twice the larger count of 1 day and 1 week earlier when one exists |
| `n1ql-latency` | warn | at least 300 queries in 60 min and an average above 200 ms with no baseline, else above twice and 50 ms above the larger average of 1 day and 1 week earlier |
| `n1ql-queue` | warn | more than 20 N1QL queries waited in the queue in 60 min |
| `auth-fail` | info | at least 20 REST requests failed authentication in 60 min |

The node checks other than `down` and `not-scraped` skip a node that is down, as the plug-in does. The baselines are part of the query (`offset 1d`, `offset 7d`, the larger of the two), so a check whose condition is in the query compares with `> 0`, and its card ends with that comparison, such as `Couchbase orders-prod connection jump: 1000 > 0.`

### Managed Redis

Reads Redis instances that you cannot reach directly, such as a managed cloud Redis, from `redis_exporter` series in a Grafana Prometheus datasource, with the checks, thresholds and texts of the [Managed Redis example plug-in](../examples/managed-redis-plugin/README.md). Placeholders: `instance` (the Grafana instance), `datasource`, `name` (such as `cache-prod`), `selector` (optional matchers, such as `env="prod"`; empty reads every Redis series, at most 80 characters), `prefix` (`redis_` by default) and `label` (the label that names each Redis, `instance` by default); change the last two only when your series are renamed. There is one card per Redis.

| Check | Severity | Fires when |
| --- | --- | --- |
| `no-series` | crit | the selector matches no `up` series (with the prefix) |
| `down` | crit | `up` is below 0.5 |
| `memory` and `memory-crit` | warn from 85 %, crit from 95 % | the peak of used memory in 30 min against `memory_max_bytes` (skipped when no limit is set) |
| `clients` | warn | the peak of connected clients in 30 min is at least 80 % of `maxclients` |
| `latency` | warn | the average command time in 30 min is at least 200 us and at least 3 times the larger average of the same 30 min 1 day and 1 week earlier; no card without either |
| `evicted` | warn | any key was evicted in 30 min |
| `rejected` | warn | any connection was rejected in 30 min |
| `restart` | warn | the uptime is below 3600 s |

A Redis that is down gets only the `down` card. The example plug-in has no hit rate, CPU or replication check, so neither has the preset; add them as your own checks when your series have them.

## A worked example

A managed Redis whose `redis_exporter` series are in a Grafana Prometheus datasource called `cloud-metrics`, on the Grafana instance `ops`. The check warns when an instance uses more than 90 % of its memory for two passes in a row:

```json
{
  "id": "redis-memory",
  "name": "Redis memory",
  "source": "grafana",
  "instance": "ops",
  "datasource": "cloud-metrics",
  "query": "100 * max by (instance) (redis_memory_used_bytes) / max by (instance) (redis_memory_max_bytes)",
  "op": ">",
  "threshold": 90,
  "severity": "warn",
  "by": ["instance"],
  "for": 2,
  "title": "{instance} memory at {value} %",
  "text": "Redis {instance} uses {value} % of its memory limit; near the limit writes fail or keys are evicted."
}
```

With `cache-a:6379` at 93.5 % for two passes, the card is titled `cache-a:6379 memory at 93.5 %`, labelled `Redis memory`, and reads `Redis cache-a:6379 uses 93.5 % of its memory limit; near the limit writes fail or keys are evicted. Redis memory: 93.5 > 90.` An instance with no `redis_memory_max_bytes` gives no series, so it is quiet.

To warn on a slow-down instead, compare with the week before: `"query": "sum by (instance) (rate(redis_commands_duration_seconds_total[5m])) / sum by (instance) (rate(redis_commands_processed_total[5m]))"`, `"op": ">"`, `"threshold": 3`, `"baselines": ["1w"]`, `"baselineMode": "ratio"` fires when the average command time is more than 3 times the one a week earlier.
