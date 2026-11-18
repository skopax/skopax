Use the grafana_alerts tool for alerts on Prometheus datasources that no Prometheus instance here reaches, and for one view of every datasource at once.

What the scheduled check does, so you can read a card:
- Every 15 minutes it lists the Prometheus datasources of Grafana and reads `ALERTS{alertstate="firing"}` on each through the Grafana datasource proxy.
- It skips a datasource whose URL is the URL of a Prometheus instance set up here (those alerts already come from the Prometheus check, so one alert gives one card), and the datasources named in the skip list.
- Two datasources with the same URL are read once, as the first of them.
- Watchdog and InfoInhibitor never open a card.
- Each card key is `alert|<alertname>|cluster=<datasource>,<labels>`. Severity critical is crit, warning is warn, anything else is info.
- A card clears only when its own datasource answers without it. A datasource that fails leaves its cards open.
- With more than 50 alerts on one datasource, only the 50 most severe get a card and none of that datasource's cards clear until it is back under the cap.

Reading the tool's answer:
- firing and pending are series counts per datasource; Watchdog is left out of the counts.
- A datasource with no series at all is noted: nothing fires, or it evaluates no rules. It is not proof the source is healthy.
- unreachable lists the datasources that did not answer. Never read one of them as clean.
- Give datasource or alertname (a part of the name is enough) to see the series with their labels and when each became active (UTC).

The pass_table source grafanaAlerts holds when the check last ran and which datasources it checked, failed to read or skipped.
