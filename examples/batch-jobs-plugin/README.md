# Batch jobs, an example Skopax plug-in

Batch jobs reads scheduled jobs, such as `export-orders`, `send-invoices`, `rebuild-search-index` and `cleanup-sessions`, through a Prometheus instance, and opens cards for jobs that fail, jobs that did not run when they usually do, and jobs that got slow. Like the [Couchbase example](../couchbase-plugin/README.md), it builds on [Hello](../hello-plugin/README.md); read Hello first. It adds a problem that a threshold on one series cannot solve: knowing when a job should have run, from its own history.

Skopax ships a fuller copy of this plug-in built in, as [Batch jobs](../../docs/plugins.md#batch-jobs), with per-job graph and log links and known causes on top. To run beside it, the example registers under its own id, `example-batch-jobs`, with its own card types, check, section and page; a plug-in id that clashes with a built-in one is refused at load. Use the built-in to watch jobs, and this folder to read how such a check is written.

## What it reads

One timer per job, as most job runners and client libraries can export it: a summary or histogram named `batch_job_duration_seconds`, with its `_count`, `_sum` and `_max` series, and three labels: the group of the job (`namespace`), the job (`job_name`) and the outcome of the run (`result`, such as `success` or `failure`). The metric, the three labels and the failure values are fields with those defaults, so a setup that writes `cron_run_seconds` with `team`, `task` and `status` changes the fields and nothing else.

The job label is `job_name` and not `job` on purpose. Prometheus sets its own `job` label (the scrape job) on every series it scrapes, so a `job` label written by the application is renamed to `exported_job`, and a `namespace` label is often replaced by relabelling, unless the scrape sets `honor_labels: true`. Check the names on a real series before you set the fields.

## What is in it

| File | What it shows |
| --- | --- |
| `pyproject.toml` | The package, and the `skopax.plugins` entry point named `example-batch-jobs` that points at `skopax_batch_jobs:PLUGIN`. |
| `skopax_batch_jobs/__init__.py` | The `Plugin`: seven fields, a Test connection, one check with a rule set, card types, card links, a knowledge entry and one page. It has no tool. |
| `skopax_batch_jobs/batch_jobs.md` | The knowledge entry for the investigator: how to read the cards and which PromQL to run. |
| `skopax_batch_jobs/panel.js` | The script of the Example batch jobs page in the sidebar's Observe group: one table per group, one row per job. |
| `skopax_batch_jobs/icon.svg` | The icon on the Integrations tile and in the sidebar. |
| `tests/test_batch_jobs.py` | Unit tests that run the check and Test connection with a fake Prometheus. |
| `Dockerfile` | A layer image: the Skopax image with this plug-in added. |
| `LICENSE` | The Apache License 2.0 text, which the wheel carries (`license-files`). |

### The plug-in, beyond Hello

- **One pass, many queries, one budget.** Every 5 minutes the check asks Prometheus for runs, failed runs, total time and the longest run of every job over the last 60 minutes, the same 60 minutes 1 day and 7 days earlier, when each job last ran (to within about 10 minutes), its runs in the last 24 hours, and the runs that the missed rule expects a day and a week ago: 16 queries, sent in parallel through `kit.promql` inside one time budget (`bounded(peer.http(), 120)`). A spent budget raises `Late`, and the pass fails as `timed out`.
- **Windows shifted for drift.** For the missed rule, the day and week windows cover the same hour as now but leave out its last `drift_min` minutes (10 by default). A job that ran at the very end of yesterday's hour and runs a minute late today is then not missed, and a daily job that ran on time just over an hour ago is not missed either.
- **Rules.** `RuleSet('rules', 'Batch job thresholds', RULES, RULE_HELP)` makes every threshold editable per instance, with a help line each:
  - a **failing** job is critical when every run in 60 minutes failed and at least 3 did, a warning when at least 2 runs failed and they are at least 20% of its runs, and info for one failed run of a rare job (at most 4 runs in 24 hours);
  - a **missed** job ran in the same window a day ago and a week ago but not now. It is critical when it ran at least 4 times in the whole same hour on both days; the level uses the whole hour, because the shorter window holds 3 or 4 runs of a 15-minute job depending on the minute of the pass. The card names the shorter window and shows the whole-hour counts that the page shows. At 3 missed jobs in one group, one card for the group replaces the cards per job, since a stopped scheduler is one problem, not ten;
  - a **slow** job's average run takes twice the slower of its two baselines and at least 60 seconds longer. It warns when the average run takes at least 50% of its interval (24 hours divided by its runs in the last 24 hours), is info from 10%, and raises nothing below that: a job that runs once a day may take twice as long without harm.
- **Counts are rounded.** `increase()` extrapolates, so 0.4 runs is no run and 0.6 is one. A job with no series now counts as no run; a job with no series anywhere is not a job.
- **The first run of a new series is not counted.** `increase()` needs two samples, so the run that creates a series (the first run of a new job, or of a job whose pod just started) shows from its second run on. Exporters that start the counter at 0 before the first run avoid this.
- **Checking the fields.** The metric, the labels, the failure values and the optional selector are pasted into PromQL, so the pass and Test connection refuse a value that is not a name, a list of words or a list of label matchers, before any query. Label values from the series go into the card links through `json.dumps`.
- **Card types and keys.** Three kinds: `example-batch-jobs-failing`, `example-batch-jobs-missed` and `example-batch-jobs-slow`. The keys are `example-batch-jobs|<instance>|<group>/<job>|<kind>`, and `example-batch-jobs|<instance>|<group>|missed` for a group card, so two instances never share a card. A `%`, `/` or `|` in a group or job is written as `%25`, `%2F` or `%7C` in the key, and the page matches cards the same way.
- **Links.** Each card carries a Prometheus graph of its own query on the peer's UI address, only when that address is `https://`.
- **No access.** A Prometheus that answers 401 or 403 raises one card built with `kit.access.card`, ends `ok` with the tail `no access to batch job metrics`, and keeps the other cards open.
- **Errors keep the cards.** No peer, a failing Prometheus, a spent budget, or no series of the metric in the last 60 minutes fail the pass, and a failed pass clears no card. A metric that disappeared, for example while its exporter is down, is therefore an error, not every job missed.
- **Knowledge.** `Knowledge('example-batch-jobs', 'Example batch jobs', ..., slack=True)` tells the investigator what each card means, what a group card usually points at, and which PromQL to run with the core `promql` tool. It reaches the prompt whole on runs about batch job cards, and is listed in the knowledge index on other runs.
- **A section and a page.** The check writes one row per job and one summary row to its `example-batch-jobs` section, and `Panel('example-batch-jobs', 'panel.js', label='Example batch jobs', group='observe')` shows them: runs, failed runs and average run against 1 and 7 days earlier, the longest run, the last run and the interval.
- **Test connection.** `test` counts the jobs and groups that have the metric's `_count` series.

## Run the tests

From the root of the Skopax repository:

```sh
.venv/bin/python -m pytest -q -n 0 examples/batch-jobs-plugin/tests
```

The page has its own suite, `tests/page/skopax-batch-jobs-panel.test.js`, which runs with the other page suites (`node tests/page/run.js batch-jobs`) and checks this page and the built-in one alike.

## Build the layer image

As for Hello, the layer image installs the plug-in in a Python build stage and copies it into the Skopax image's `site-packages`:

```sh
docker build --build-arg SKOPAX_VERSION=0.5.10 -t skopax-batch-jobs examples/batch-jobs-plugin
```

Pin `SKOPAX_VERSION` to the Skopax release you run. Example batch jobs is then listed in Connect > Integrations next to the built-in Batch jobs; add a Prometheus instance first, then one instance per set of jobs.

## Licence

Apache-2.0, like Skopax (see `LICENSE`). Copyright 2026 Maksut Spahi.
