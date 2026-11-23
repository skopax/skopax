`example-batch-jobs` reads scheduled batch jobs through a Prometheus instance, from one timer per job: `batch_job_duration_seconds_count`, `_sum` and `_max`, with a label for the group of the job (`namespace` by default), one for the job (`job_name`) and one for the outcome of a run (`result`, such as `success` or `failure`). The admin can rename the metric and the three labels per instance, so the names below are the defaults and may differ.

- Cards: `example-batch-jobs-failing` (failed runs in the last 60 min: critical when every run failed, a warning on a share of failed runs, info for one failed run of a rare job), `example-batch-jobs-missed` (a job that did not run in the last 60 min but ran in the same hour a day ago and a week ago, less the last few drift minutes of that hour; the card shows the counts of that shorter window and of the whole hour, and its level uses the whole-hour counts; a card for the whole group when many jobs of one group miss at once) and `example-batch-jobs-slow` (the average run takes several times its usual and a real share of the time between runs).
- For missed jobs the day and week windows leave out their last few minutes, so a job that starts a little late is not missed. A missed card on the hour a job is due may still clear on the next pass.
- A group card for missed jobs usually means the scheduler of that group stopped, its pods cannot start, or the job pods are no longer scraped. Look at the scheduler and at the metrics first, not at the jobs one by one.
- A failing job with steady run times points at its input or at a service it calls; a failing job whose runs got much shorter fails early, often on start-up, a missing secret or a refused connection.
- A slow job that takes most of its interval will soon overlap its next run. Check whether runs pile up (runs in 24 h fall while the average rises).

Use `promql` on the same Prometheus instance, with the metric and label names of the instance:

- Runs and failed runs of one job: `sum by (result) (increase(batch_job_duration_seconds_count{namespace="shop",job_name="export-orders"}[60m]))`
- The same window a day and a week ago: add `offset 1d` or `offset 7d` after the range.
- Average run time: `sum(increase(batch_job_duration_seconds_sum{job_name="export-orders"}[60m])) / sum(increase(batch_job_duration_seconds_count{job_name="export-orders"}[60m]))`
- Longest run: `max(max_over_time(batch_job_duration_seconds_max{job_name="export-orders"}[60m]))`
- When it last ran, to within about 10 min: `max(max_over_time(timestamp(increase(batch_job_duration_seconds_count{job_name="export-orders"}[10m]) > 0)[1d:5m]))`
- All jobs of a group, runs per hour over two days: a range query of `sum by (job_name) (increase(batch_job_duration_seconds_count{namespace="shop"}[1h]))`.
