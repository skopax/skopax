Use the esql tool for log lines, error messages and traces that the cards and tables cannot show. It runs one ES|QL query against one Elasticsearch instance. The cluster input picks the log cluster (logs, the default) or the APM cluster (apm) inside that instance; when several instances are configured, the instance input picks one and the primary answers without it.

What the scheduled checks read, so you can reproduce a card:
- Log patterns (logs): ERROR and WARN lines of the last 60 minutes, grouped by service, level and the first 90 characters of the error text, else the message. The level is compared in upper case. The same hour one day and one week earlier is the baseline.
- A pattern card opens for a new or growing ERROR pattern (5 or more lines and more than twice the baseline plus 3), or a loud WARN pattern (30 or more lines and five times the baseline). The card key is log|service|level|message, with ids replaced by <id>.
- APM (apm): request transactions of the last 60 minutes per service.name in the APM index. A p95 card is info only and needs the APM rule set's factor and floor over the baseline; a failure card needs 10 or more failed transactions and a failed share over twice the baseline plus half a percent. Services deployed in the last 30 minutes are skipped.
- Log flow (logflow): log lines per namespace, taken from the index name (index-namespace) or the namespace field, in the last 20 minutes against the same 20 minutes one day and one week earlier. A namespace with lines in both earlier windows and none now gets a card, and so does the pipeline when the newest line of a busy namespace is late or all lines together fall far below both earlier windows.
- While a log flow card (logs-stopped, logs-lag) is open, every log-based card and answer for that namespace is incomplete: say so before concluding "no errors". When logs ship through a queue, its consumer lag delays what you read.
- Never count traffic from APM: transactions are sampled and messaging spans can drop out for hours. Take volume from metrics or log lines; use APM for p95 and the failed share. APM service names and service.environment values are often inconsistent (hyphens or underscores, one environment per namespace): group by them once before you filter on one value.
- Full URLs in traces and logs can carry personal data in the query string: test for it with LOCATE(field, "needle") > 0 as a boolean and never print the values.
- "Is anyone still calling route X?" is answered from the ingress access log, not the app logs. Keep scans of a busy access log to one named day per query, with the host filter first, and compare a status field as its mapped type (a text status needs "404").
- When a Kibana URL is set, cards carry their Kibana links: reuse them, or give the ES|QL text. Never build a Kibana URL by hand.

Query tips:
- Start with FROM and name the indices. A wildcard, or more than three indices, needs a @timestamp bound in WHERE, such as WHERE @timestamp >= NOW() - 1 hour.
- A query that times out (HTTP 504, or no answer in time) over more than one day, with a lower bound like WHERE @timestamp >= NOW() - 3 days, is retried once on the last day only (NOW() - 1 day); the answer then says so, and you ask for the earlier days one at a time. A query without such a range is not retried: narrow the time range or the index.
- Aggregate with STATS ... BY instead of reading raw lines; keep LIMIT small when you need lines.
- first and last are reserved words: name such columns oldest and newest.
- A field compared with a number that answers "is [text]" needs a quoted value or its .keyword field.
- Full messages are nearly unique: group on a prefix, such as STATS c = COUNT(*) BY msg = SUBSTRING(message, 1, 80); COALESCE the exception message and the message when both shapes occur.
- A window across midnight on daily indices names both days in FROM. Wide or multi-day scans: filter on the logger name and level, not a `message LIKE` over every index (the slowest shape); find the logger with a one-day LIKE first. FROM ... METADATA _index and BY _index split rows per index.
- COUNT_DISTINCT of the pod next to COUNT(*) tells a once-per-pod-start line (count = pods times lines per start, as after a rollout) from steady noise. For a rate, count the message prefixes of the attempt, the success and the failure in one STATS, so the denominator comes from the same query.
- LIKE treats `?` as one character, so LIKE "/?*" matches every path; use STARTS_WITH for a literal `?`. A `\"` inside a quoted literal fails: use a triple-quoted pattern. Inside EVAL, compare @timestamp with TO_DATETIME("...Z"), not a bare string.
- Query-parameter values: EVAL r = CONCAT(url, "&") | DISSECT r """%{}amount=%{amt}&%{}""", so the last parameter parses too.
- Times are UTC.
- Instance options change what a card means: logflow-index (the index the log flow check reads, else log-index), message-mask (matches are replaced by <masked> in the first 400 characters of a line before grouping, so a pattern key can end at <masked>), apm-p95-overrides (a service with its own p95 factor and floor) and discover-columns (extra columns in Discover links).

Before you trust an empty answer:
- A field that is mapped but empty for these rows returns no rows, not an error, and logs may not follow ECS (no `service.name` or `log.level`): run one `| LIMIT 1` with no other filter (a wildcard still needs its @timestamp bound) to see the field names first. A zero-row answer counts only after the same query returns rows for something you know is live.
- Services that do not log structured JSON (sidecars, some Node or Python apps, access logs) leave level, message and exception fields empty and keep the whole line in one raw field, so a level == "ERROR" scan returns nothing or one null row. Group on the raw line and take the severity from its text. Many apps log success silently: prove a job ran from the data it wrote, not from missing lines.
- A `.keyword` field is empty for values longer than its ignore_above limit (long URLs, referrers): use SUBSTRING, LIKE or DISSECT on the text field.

Forensics:
- The cause chain is text inside the stack trace field: DISSECT stack """%{}Caused by: %{cause}: %{}""". Frame line numbers tell an old build from a new one during a rollout.
- EVAL via_x = LOCATE(field, "needle") > 0 | STATS c = COUNT(*) BY hour = DATE_TRUNC(1 hour, @timestamp), via_x splits an old and a new code path across a deploy.
- One exception can be logged by a shared error handler with its own wording and level: before you say what level something logs at, search its message key across every logger and group by level and logger. Follow one request across services by its trace id before blaming the service that logged the last error.
- "Many customers at once" is distinct users per short window, not hits per hour: EVAL b = DATE_TRUNC(10 minutes, @timestamp) | STATS u = COUNT_DISTINCT(user) BY b, and again with the grid shifted by 5 minutes so a burst on a bucket edge is not split; take the larger. A signal that flaps needs several quiet 5-minute buckets before you call it over.
- Services that log requests only on WARN and ERROR cannot tell a new failure from a feature nobody uses: take the denominator from the access log (status by client build over two or three weeks).
- In Spring WebClient errors, a DecodingException whose trace starts in the JSON decoder with `Body from UNKNOWN [DefaultClientResponse]` means the provider answered 2xx and its body did not fit our response type; a missing creator parameter names the absent field. The body is rarely logged: say who must get it from the provider.
- A rollout is clean when each new pod logs one startup line and nothing else; a startup error repeated per pod is a crash loop, with the cause chain at the end of that message. Don't blame a job that ran in the same minute until its writes land inside the failing window.
