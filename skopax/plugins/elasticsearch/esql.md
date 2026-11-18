Use the esql tool for log lines, error messages and traces that the cards and tables cannot show. It runs one ES|QL query against one Elasticsearch instance. The cluster input picks the log cluster (logs, the default) or the APM cluster (apm) inside that instance; when several instances are configured, the instance input picks one and the primary answers without it.

What the scheduled checks read, so you can reproduce a card:
- Log patterns (logs): ERROR and WARN lines of the last 60 minutes, grouped by service, level and the first 90 characters of the error text, else the message. The level is compared in upper case. The same hour one day and one week earlier is the baseline.
- A pattern card opens for a new or growing ERROR pattern (5 or more lines and more than twice the baseline plus 3), or a loud WARN pattern (30 or more lines and five times the baseline). The card key is log|service|level|message, with ids replaced by <id>.
- APM (apm): request transactions of the last 60 minutes per service.name in the APM index. A p95 card is info only and needs the APM rule set's factor and floor over the baseline; a failure card needs 10 or more failed transactions and a failed share over twice the baseline plus half a percent. Services deployed in the last 30 minutes are skipped.
- Log flow (logflow): log lines per namespace, taken from the index name (index-namespace) or the namespace field, in the last 20 minutes against the same 20 minutes one day and one week earlier. A namespace with lines in both earlier windows and none now gets a card, and so does the pipeline when the newest line of a busy namespace is late or all lines together fall far below both earlier windows.

Query tips:
- Start with FROM and name the indices. A wildcard, or more than three indices, needs a @timestamp bound in WHERE, such as WHERE @timestamp >= NOW() - 1 hour.
- Aggregate with STATS ... BY instead of reading raw lines; keep LIMIT small when you need lines.
- first and last are reserved words: name such columns oldest and newest.
- A field compared with a number that answers "is [text]" needs a quoted value or its .keyword field.
- Times are UTC.
