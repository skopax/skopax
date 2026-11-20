# Couchbase, an example Skopax plug-in

Couchbase reads a Couchbase cluster through a Prometheus instance that scrapes the Couchbase exporters, and opens cards for down nodes, restarts, connection jumps, index memory, rebalances, auto-failovers and N1QL errors, timeouts and time. It is the fuller sibling of [Hello](../hello-plugin/README.md): where Hello shows each part of the plug-in contract once, this example shows the parts a real integration needs, such as a peer instance, baselines, rules, card types, links, a page and an investigator tool. Read Hello first.

The `couchbase` preset of [custom checks](../../docs/custom-checks.md#presets) now covers the common case without a plug-in; this example stays as an example of a full plug-in.

## What is in it

| File | What it shows |
| --- | --- |
| `pyproject.toml` | The package, and the `skopax.plugins` entry point named `couchbase` that points at `skopax_couchbase:PLUGIN`. |
| `skopax_couchbase/__init__.py` | The `Plugin`: three fields, a Test connection, one check with a rule set, card types, card links, one read-only tool and one page. |
| `skopax_couchbase/panel.js` | The script of the Couchbase page in the sidebar's Observe group: one block per cluster, one row per node. |
| `skopax_couchbase/icon.svg` | The icon on the Integrations tile and in the sidebar. |
| `tests/test_couchbase.py` | Unit tests that run the check, Test connection and the tool with a fake Prometheus. |
| `Dockerfile` | A layer image: the Skopax image with this plug-in added. |
| `LICENSE` | The Apache License 2.0 text, which the wheel carries (`license-files`). |

### The plug-in, beyond Hello

- **One instance per cluster.** `max_instances` is left unset, so an admin adds one instance per cluster in Connect > Integrations. The card keys hold the instance name, `couchbase|<instance>|<node or cluster>|<kind>`, so two clusters never share a card, even when they have a node of the same name.
- **A peer instance.** The plug-in opens no connection of its own. `uses=('prometheus',)` and the field `Field('prometheus', 'Prometheus', 'instance', choices=('prometheus',))` let the admin pick one of the Prometheus instances, and `ctx.peer('prometheus')` gives the check its settings and transport. Without a peer the pass fails with `no Prometheus instance is set`, and every card stays open.
- **Fields instead of fixed values.** `cluster` is the name the cards and the page show. `selector` holds the label matchers that pick the cluster in every query, `job=~".*couchbase.*"` by default. The nodes come from the `up` series that the selector finds, so a node added to the cluster shows on the next pass.
- **Checking a field.** A field's value reaches PromQL inside `{...}`, so the plug-in checks the whole selector before it sends a query: it must be a list of `label op "value"` matchers, joined by commas. A selector such as `job="a"} or up{x="1"` would read other series, and the pass and Test connection refuse it with one sentence. Skopax checks a field's kind when the instance is saved, but not a plug-in's own rules for it, so this check runs in the pass and in Test connection.
- **Baselines.** `kit.promql` reads each query three times: now, 1 day earlier and 7 days earlier (`client.vec(expr, at)`), in parallel and inside one time budget (`bounded(peer.http(), 200)`, which raises `Late` when it is spent). The N1QL error share, the N1QL timeouts and time, and the connection peak of each node are compared with the same 60 minutes of those two days. Until a baseline exists, a fixed floor applies, such as 1% of errors or 200 ms of average time. A node that had an `up` series a day ago and has none now gets a `not scraped` warning.
- **Rules.** `RuleSet('rules', 'Couchbase thresholds', RULES, RULE_HELP)` makes every threshold editable per instance, with a help line each. The check reads them as `{**RULES, **ctx.rules('rules')}`.
- **Card types.** `card_types` names the eleven kinds (`couchbase-down`, `couchbase-n1ql-errors`, ...), so the board shows `N1QL errors` instead of the raw id. Each kind starts with the plug-in's id, so no other plug-in names the same kind.
- **Links.** Each card carries a Prometheus graph of its own query, built with `kit.promql.prom_link` on the peer's UI address. `links(card, info)` hands them to the card page, and only `https://` addresses are kept: a Prometheus behind plain HTTP gets no links.
- **No access.** A Prometheus that answers 401 or 403 does not mean the cluster is fine. The pass raises one card built with `kit.access.card`, says `ok` with the tail `no access to Couchbase metrics`, and lists only `couchbase:access:metrics` in `ran`, so the other Couchbase cards stay open until a pass can read again.
- **Errors keep the cards.** When Prometheus fails or returns no `up` series for the selector, the pass fails (`ok` is false). A failed pass clears no card.
- **A section and a page.** The check writes one row per node and one summary row per cluster to its `couchbase` section, and `Panel('couchbase', 'panel.js', label='Couchbase', group='observe')` shows them in the Observe group. Skopax adds `cluster` (the instance name) to the rows and cards of every instance but the primary one, and the page groups by it.
- **A read-only tool.** `Tool('couchbase_nodes', ...)` gives the investigator the node table of the last pass, read with `ctx.previous('couchbase')`, so it costs no query. Its JSON schema takes `node` (part of a node name) and `limit` (1 to 50), the rows are capped at 50 with a note that says so, down nodes come first, and `per_run=4` caps the calls in one investigation. `{field:cluster}` and `{field:prometheus}` in the description are filled with the primary instance's values for the model.
- **Test connection.** `test` asks the peer for the `up` series once and says how many nodes are up, or which part refused the read.

## Run the tests

From the root of the Skopax repository:

```sh
.venv/bin/python -m pytest -q -n 0 examples/couchbase-plugin/tests
```

## Build the layer image

As for Hello, the layer image installs the plug-in in a Python build stage and copies it into the Skopax image's `site-packages`:

```sh
docker build --build-arg SKOPAX_VERSION=0.4.1 -t skopax-couchbase examples/couchbase-plugin
```

Pin `SKOPAX_VERSION` to the Skopax release you run. Couchbase is then listed in Connect > Integrations; add a Prometheus instance first, then one Couchbase instance per cluster.

## Licence

Apache-2.0, like Skopax (see `LICENSE`). Copyright 2026 Maksut Spahi.
