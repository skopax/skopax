# Changelog

Skopax follows semantic versioning. Releases are listed newest first.

## 0.5.17 (2026-11-25)

A short summary of each card's Slack thread on the card, a model per severity for card investigations, Claude Fable 5.2 and Mythos 5.2, and an Integrations strip that stays closed.

### Added

- Slack thread summary: when people discuss a card in its Slack thread, the card gets a short summary of corrections, decisions and open questions, updated after Skopax answers and when the thread goes quiet. The investigator reads it as claims to check, not as instructions. Thread lines are masked for secrets before they are sent, attempts count toward a cap of 6 per card per day, and Admin can switch it off.
- Card investigations can use a different model for Critical and High cards (Admin > AI: Model for Critical cards, Model for High cards), for example Claude Fable 5.2 for Critical only; empty keeps the Card model.
- Claude Fable 5.2 and Claude Mythos 5.2 can be picked for every AI job (Mythos needs an Anthropic organization that is verified for it). Their spend is counted at $8 / $40 per million tokens, and their calls keep thinking on, as these models require.

### Changed

- The Integrations strip above the board starts closed, also when a source has an error; the pill turns red and names the failing source, and one click opens the full list.
- Slack thread reads page through long threads instead of stopping at the first 200 messages, so answers in long threads see the newest messages.

## 0.5.16 (2026-11-25)

A one-row board filter bar instead of More filters, a warning mark on tiles and list rows, no severity stripes, and fixes for Mark all read, tag-row icons and the Warning badge in emails.

### Changed

- The board filters are one row: Open, Mine, Critical and Done, then Status, Tags, Assignee and Type lists with counts and search, and the Board/List switch; More filters is gone. Several picks in one list show cards matching any of them; picks in different lists must all match.
- Board tiles and list rows show a small warning mark when a card has a Slack post that failed or was held back, a page that is gone, or a back tag; hover names it.
- Board tiles and Closed list rows no longer carry a severity stripe; severity colour is on the severity badge only, and Closed rows now show that badge.
- In emails a Warning badge is outlined in yellow instead of filled, so a mail app in dark mode can no longer make it look like High.

### Fixed

- Mark all read in Notifications works again while the shown items still look unread.
- Icons in the opened card's tag row are small again.
- An alert card with no service (for example a batch-job alert that carries only a namespace) no longer gets an Elasticsearch Logs link that searches for the service "-" and finds nothing; deploy report rows without a service get none either.

## 0.5.15 (2026-11-24)

A calmer opened card: a short More menu, one short last-action and status line, at most three tags in view and long blocks folded. Also picking the agent node from a list, and the Skopax link opening in a new tab.

### Changed

- More on an opened card is a short menu in five groups (status, AI, ticket, card, send) that works with the keyboard (arrow keys, Home, End, Enter, Esc). Every action is still there. Copy link and sends show their result as a short message.
- The card header shows number, title, severity, state and owner. The last action is one short line ("Done 02:33 - stopped firing"); the full text opens under it. The state line is short too ("Stopped firing - <owner>"), with the full sentence on hover.
- At most three tags are in view, warnings first, with "+N" for the rest; triage, Recurring and team tags are under Technical details, and so are the Slack and Teams status lines. Investigate is no longer a filled button.
- Long text blocks on a card (follow-up, investigation note, recap, runs, clear note, Slack draft) are folded, and each kind remembers in the browser whether you opened it. History shows one line per entry with Show more when a line is longer.
- List rows use the same layout as the opened card plus an Age column; tags and change are under Technical details.
- Through agent node (Elasticsearch and other plug-ins that opt in) and the Kubernetes Relay field are now a list of the Kubernetes agent instances instead of free text, with None to connect directly. A saved name that is not an agent node is shown as such.
- The Skopax link in the left menu and the page footer opens in a new tab.

## 0.5.14 (2026-11-24)

A simpler board: cards open expanded on the board instead of on a page of their own, fewer controls in view, a calmer blue palette and the left menu collapsed on a first visit. Also Elasticsearch connections reached through an agent node, longer Slack thread context with earlier screenshots, and Spring Config missing keys logged once.

### Changed

- Cards open in the side panel on the board, wide by default. The top reads "What happened" and "What to do"; the main buttons are Take it, Assign, Resolve and Ask AI, and every other action is under More. Technical details (cause, numbers, suspects, key, follow-up, screenshots, merged cards, runs, Slack draft) are folded, and the panel remembers if you open them.
- The board shows Open, Mine, Critical and Done; every other filter, the people and tag chips, the card types and the Board/List switch are under More filters, which opens by itself and shows a count when a filter inside it is on. The separate filter column is gone.
- Board tiles show the severity dot, title, number, age and owner, plus a live Investigating tag; the hover action row is gone and dragging between columns still moves a card.
- A calmer palette in both themes: blues from the logo, neutral pills and tags, severity colour only on the dot, and a light left menu in the light theme.
- The left menu starts collapsed on a first visit; a saved choice wins.
- In a Slack thread, the last three messages from people before a mention are read in full (up to 4000 characters each, longer ones marked as cut), and screenshots in those messages are read too, up to three per question.
- Chat answer ready is no longer mailed by default. On upgrade it is unticked once in saved mail choices too; a person who wants it ticks it again in Preferences.
- Spring Config logs a key missing from the config server answer once per process instead of on every read, and Admin lists the left-out keys.

### Added

- Through agent node: an Elasticsearch connection (and any plug-in that opts in through its agent read) can name a Kubernetes agent instance that reaches it. Its queries go through that node, its settings are pushed to the node in memory only, and it runs no automatic checks. The esql tool picks the connection with `instance`.

### Removed

- The separate card page. Old card links (`/c/<num>`) still work: they open the board with that card expanded, also for closed and older cards.

## 0.5.13 (2026-11-24)

Zabbix and CloudWatch pages in the left menu, a quiet first pass for newly connected Zabbix and CloudWatch instances, Slack mentions for people assigned by name, Admin switches that show what was saved on every pod, and Spring Cloud Config keys that fill several fields or build one from several keys.

**Added:**

- Zabbix page in the left menu: the hosts of the watched host groups, grouped by group, with interface availability and its error text, maintenance and open problems by severity, and the open problems at or above the lowest severity with severity, host, start, acknowledged, the card and a link to the event in Zabbix. Host group, severity and text filters and an instance switcher that also lists instances not read yet. Reads are bounded (300 hosts, 200 problem rows, 200 KB per instance, trimming problems below High first, then hosts, then High and Disaster); a failed page read keeps the earlier rows with their time.
- CloudWatch page in the left menu: per instance the region with its ALARM, INSUFFICIENT_DATA and OK alarm counts, and the alarms in ALARM or INSUFFICIENT_DATA with type, reason, since, the card and an AWS console link (console.amazonaws.cn for cn-* regions); per watched log group the error lines of the last 15 minutes and of about the last hour with the latest sample lines. Region, state and text filters and an instance switcher. The page starts no Logs Insights query; the counts take up to 10 extra DescribeAlarms calls per instance every 15 minutes.
- Spring Cloud Config: one config key can fill several targets (`key=target1|target2`, or the key repeated), so a shared credential is stored once and a rotation reaches every copy; a key's targets must be all secret fields or none. New `SPRING_CONFIG_TEMPLATES` builds one secret field from several keys with `${key}` placeholders that take raw config values (no environment, defaults or nesting); a missing key leaves the field unset and the log names the target and the key, never a value.
- Plug-in contract: a `settings_source` hook can declare an optional `field_kind` parameter; core then passes `field_kind(plugin_id, field)`. Hooks that take only `env` are called as before.

**Changed:**

- Connecting a Zabbix or CloudWatch instance no longer floods the board. A problem that started more than 1 hour before the instance was connected still opens its card, which says "Already active when <plug-in> was connected (since <time>)", but it is not auto-investigated, not auto-assigned, not pushed and not posted by Slack, Teams or email routes, and it stays out of the warning digest; a person can still press Investigate. A later escalation, a problem coming back with a new start or after it was resolved as fixed, behaves as usual. The instance counts as connected when it was created or switched on (a switch-off of up to 24 hours keeps the earlier time), or else at its first read; instances from before 0.5.13 count as connected long ago.

**Fixed:**

- A card assigned by name only now mentions the person in the Slack thread note and reaches them by direct message when the guessed address is unknown to Slack but exactly one Slack member has that name. The Slack member list loads at start, and an assignment waits up to 3 s for it once. Several members with that name still mean no mention, with one throttled warning that names neither the person nor an address.
- Switching a plug-in, an instance or a feature on or off in Admin no longer redraws the old value when another pod answers the follow-up read: the lists read the on/off flags from the database on every request, and the page keeps what the save answered while the read is older than the save.
- A Prometheus alert with a null `activeAt` no longer fails the pass.

## 0.5.12 (2026-11-24)

Grafana Loki and AWS CloudWatch plug-ins, OpenTelemetry traces stored natively in Elastic, clearer ES|QL hints for the investigator, Take now assigns the card to whoever presses it, Jira tasks whose fields are all set in Admin or asked in the dialog, and Jira create errors that say which field was refused.

**Added:**

- Loki plug-in: error log patterns per service from Grafana Loki (self-hosted or Grafana Cloud; none, bearer or basic auth; optional tenant as `X-Scope-OrgID`) become cards keyed `log|<service>|<ERROR or WARN>|<pattern>` with the Elasticsearch log rules, from per-service `count_over_time` counts, a 200-line sample grouped by normalised pattern and exact counts against a day and a week earlier; read tools `loki_query`, `loki_count` and `loki_labels` always stay inside the instance's stream selector.
- CloudWatch plug-in: AWS CloudWatch metric and composite alarms in ALARM become cards, one per alarm, keyed `cloudwatch|<region>|<alarm name>`, with severity from words in the name or description (crit or critical Critical, warn or warning Warning, otherwise High; adjustable), the service from the alarm's dimensions, and a card that clears when the alarm leaves ALARM; optional name prefixes narrow it and an optional switch also carries alarms in INSUFFICIENT_DATA, and optional Logs Insights counts error, exception and fatal lines per log group over the last 15 minutes (off while no log groups are set, since it is billed per GB scanned). Requests are signed with SigV4 by Skopax itself (no AWS SDK), with credentials from access keys or IRSA (web identity), optionally followed by an assumed role; it uses only read actions, no redirects and at most 500 alarms a pass. The investigator gets `cloudwatch_alarm`, `cloudwatch_metric` and `cloudwatch_logs`.
- Elasticsearch: the APM check also reads OpenTelemetry traces that Elastic 8.16+ stores OTel-native (EDOT collector or managed OTLP endpoint). The default `apm-index` is now `traces-apm*,traces-*.otel-*`, and each shape gets its own ES|QL query: `kind`/root spans, `status.code` and `duration` in nanoseconds for OTel, the usual transaction fields for classic APM. The same `apm-fail` and APM p95 cards come out in milliseconds. Test connection says which shapes it found, and classic-only installs see no change.
- Jira Create task asks Jira what a new task needs: the scope field's allowed values (with `jira.dev-scope` picked first), an "Add to the current sprint" box set by the new `jira.sprint-default` (default on), and an input for every other required field without a default, which the server checks against the project's create screen (cached 5 minutes). The scope, points, developer and sprint field ids are now Admin settings under Jira tasks > Fields, with a picker that lists Jira's fields by name; installs that set these field ids in code can move them to Admin.

**Changed:**

- Take moves the card to the person who presses it, even when it was assigned to someone else (by hand or by auto-assign); taking it for someone named picks that person. Before, Take kept the existing assignee.
- Elasticsearch and Loki share `log|...` card keys: the first of them to open a card keeps it while it is open; the other's items for that key no longer take it over, escalate or clear it, and once its owner cleared it the other may reopen it as the new owner.
- The chat logo for a custom workspace name draws its text in Helvetica or Arial instead of the system font, which some browsers rendered as wrong letters inside an image.
- Test connection asks first for every plug-in whose test posts a real message (`Plugin(test_posts=True)`), with wording that names the plug-in.

**Fixed:**

- The investigator's ES|QL tool explains two common mistakes: comparing a text or keyword field with a number (it names the field and suggests a string or TO_INTEGER) and a GROK or DISSECT pattern with inner quotes (use triple quotes).
- Creating a Jira ticket that Jira refuses with HTTP 400 shows Jira's own reasons, such as the required field it is missing, masked and cut to 500 characters; the log names only the refused field ids.

## 0.5.11 (2026-11-24)

A Zabbix plug-in, and a read-only Integrations list that shows only what is connected.

**Added:**

- Zabbix plug-in: active Zabbix problems (6.0 LTS and later) become cards, one per trigger, keyed `zabbix|<host>|<trigger description>|<trigger id>`, with Zabbix severities mapped (Disaster Critical, High High, Average and Warning Warning, Information and Not classified Info). Acknowledged problems stay with a note, tags fill the card text and a `service` or `component` tag its service, and a card clears when the problem is gone. Optional host groups, tags and lowest severity narrow it. It uses an API token in the Bearer header (also in the body below 6.4), only read methods, no redirects and at most 500 problems a pass. The investigator gets `zabbix_problem`, `zabbix_host` and `zabbix_history`.

**Changed:**

- The read-only Integrations list (people who are not admins, and admins previewing as a viewer) shows each integration's icon, name, category, description, state and how many instances are connected, and nothing opens on click. Readers no longer get instance names, fields, setup, checks, tools or problem texts; each state reads as one fixed sentence, such as "Needs attention: an admin can see the details".

**Fixed:**

- A business sentence gives times as a date and a clock time (such as 23 Nov 10:00) instead of today, tonight or yesterday, since it is shown again on later days and on other cards' posts.
- The investigator's business sentence is refused, and written again, when it says or implies that someone is following, looking into or fixing the problem, or when it uses today, tonight or yesterday (English, Turkish, German, Spanish and French).

## 0.5.10 (2026-11-23)

Routes wait for the investigation and never back-fill, tag responsibles get their part by mail, waiting cards are re-checked and closed with a short announcement, a Vault settings source, and a confirmation before a Teams test card. Read the upgrade notes under Changed.

**Added:**

- Tag responsibles get their part by mail through the Mail server, apart from Slack. Each responsible gets one mail a day with the open cards of their tags (the daily tag report), one with the waiting Warning cards of their tags (the daily warnings digest), and the plug-in digests that name one of their tags. The switch is Mail responsibles their part (`tags.mail`, `SKOPAX_TAGS_MAIL`, on by default) in Admin > Settings > Card tags. The mails go out even while Slack is not set up or Slack auto-send is off, once a day across all pods. A responsible with nothing in their tags gets no mail. Mails show each card once, with a coloured severity label and an Open the board button.
- Follow-up checks: when the investigator's next step is for a person outside Skopax (a redirect, an ad link), it can record a done-condition. Skopax re-checks the waiting card with read tools an hour later and then every few hours (`investigate.follow-up-hours`), and gives up after `investigate.follow-up-days`. Until then the card is not auto-resolved as No action needed.
- Settle review of waiting cards (Admin > Features > Settle waiting cards, on by default): stopped cards that people took or were assigned to (investigated or not), and that are past their auto-resolve window, get a short investigator review with one of four verdicts: fixed, no action, keep or ticket.
- Announced auto-close: a fixed or no-action verdict posts one line in the card's Slack thread (when it has one), mentions the assignee, adds a Closes soon notification and shows the close time on the card page, with the day when it is not today. The card closes after `investigate.follow-up-close-hours` (business hours) as Fixed or No action needed, unless it fires again, a person acts on it or writes a note on it, or someone presses the new Keep open button on the card page or in Slack. A card with nobody to tell keeps the earlier auto-resolve rule.
- Follow-up checks never close a card on a done-condition written by a run that read planted text, and a verdict must rest on a successful read in that run that matches its claim.
- The card page shows a Follow-up check line and the pending close. History lists checks, announced and cancelled closes, and Keep open. MCP `get_card` shows the follow-up line.
- New settings: `investigate.follow-up-hours`, `investigate.follow-up-days`, `investigate.follow-up-close-hours`, `investigate.settle-max-per-day` (the daily cap on checks and reviews, which also count against the daily limit) and `routes.wait-minutes`.
- Vault settings source: a built-in plug-in reads Skopax settings and integration instances, secrets included, from HashiCorp Vault or OpenBao (KV version 2), with token, Kubernetes or AppRole sign-in. It is set by `VAULT_*` environment variables only, only reads from Vault, and keeps its last good data when Vault is down or refuses the read. Admin shows the fields Vault owns as read-only and never shows its secrets. See docs/plugins.md, Vault.
- Test connection on a Microsoft Teams route asks first, since it posts a real card to the channel that people there see.

**Changed:**

- Teams, Email and other routes now wait for the investigation. A card below Post immediately from is sent once the investigator has finished it, with a "Finding:" line. With no finished investigation after `routes.wait-minutes` (default 30, 5 to 240, Admin > Settings > Slack) the plain card is sent. Cards at or above Post immediately from, and every card while the investigator is off, paused, capped or not live, go at once. A held card that stops firing while still unresolved is sent as the plain card at once.
- Also post to Slack follows the same timing, so a route no longer posts a bare card to the main channel early; an investigated card's Slack post is filled with the finding at once.
- Every mail ends with the address of your install (the host of the base URL, linked), then the "Sent automatically" line, and, smaller, the "powered by Skopax" line, which the Enterprise switch still hides.
- Upgrading to 0.5.10: routes hold cards as above; long-waiting taken or assigned cards are reviewed gradually under the daily cap, oldest first. The auto-resolve rule for cards nobody acted on is unchanged.

**Fixed:**

- A route sends only cards that are new, escalated or fire again after it is set up or changed. Creating or editing a route, or the one-time High re-score, no longer mails or posts every open card that newly matches. A post that failed is still retried after the route is edited.
- Route sends show in the History tab of every card they covered.
- Slack reminders and the assigned note mention an assignee known only by name (for example picked from Jira) when exactly one Slack member has that name; otherwise the name stays plain text.

**Plug-ins:**

- `Digest(title, lines, tags=(...))` can name card tags; the digest is then also mailed to the responsibles of those tags, one mail per person, with the subject "[Skopax] <title>", and shows in Activity as `check-digest-mail`. Unknown tag ids are left out with one warning; tags that are not a tuple of texts fail the pass.
- `skopax.remote.pem_sender(pem, follow, limit, proxied)` gives a transport that trusts a given PEM certificate authority.
- `Plugin(test_posts=True)` marks a plug-in whose test posts a message others see; the page asks before running it.

## 0.5.9 (2026-11-23)

More cards lean to High, a daily Warnings digest, Batch jobs built in, Turkish identity and phone masking, MCP servers over plain http inside the cluster, email notifications on by default, and fixes for several pods. Read the upgrade notes under Changed: Warning cards that today's rules make High become High once, and personal notification mails are now on for everyone who has signed in.

**Added:**

- Daily warnings digest: on business days Skopax posts one message to the main Slack channel listing the open Warning cards nobody is assigned to or working on, grouped by service, up to 15 with "+N more", and a link to the board's Warning view. Time in Warnings digest at (`slack.warn-digest-at`, `SLACK_WARN_DIGEST_AT`, default `09:30`; empty turns it off), switch Daily warnings digest (`core.warn-digest`, `SKOPAX_FEATURE_WARN_DIGEST`); it also needs Slack auto-send on. Cards with a ticket or in progress are left out.
- Batch jobs ships built in: failing, missed and slow scheduled jobs read from one timer per job in Prometheus every 10 minutes (formerly the batch-jobs example, which ran every 5 minutes; instances and cards carry over).
- Batch jobs: optional Job graph link and Job logs link templates with `{job}` and `{namespace}`, shown on each job's cards and given to the investigator, and Known causes, short notes per job pattern that show on the card as Known cause and reach the investigator as a hint from the team.
- Redaction masks Turkish national identity numbers (11 digits that pass the official checksum) and Turkish mobile numbers in the national format, such as 0532 123 45 67, (0532) 123 45 67 and +90 532 123 45 67. Numbers written as `532 1234567` or `0 532 1234567` stay visible, since they look the same as metric and table rows.
- Secret values are also found by secret words in other languages (for example sifre, parola, Passwort, contrasena, mot de passe), read from a secret-words.txt file in each built-in language pack.
- The MCP server plug-in accepts plain http for a service in the same Kubernetes cluster (`name.namespace.svc` or `name.namespace.svc.cluster.local`). Such a name is looked up as an absolute cluster name, must resolve to private addresses only, and redirects stay off; everything else stays https.
- MCP server setting Calls to MCP servers per run (`mcp-tools.run-calls`, default 20, 1 to 60); calls refused before they reach a server do not count.
- Extra token request headers (`sso.extra-token-headers`) on the Single sign-on card: up to 10 checked `Name: value` headers sent only on the sign-in token request; plain settings, not for secrets. `SSO_TOKEN_HEADERS` keeps working as before.
- The reachability probe also checks Slack, the AI provider in use (its custom base URL when one is set) and the Google API hosts of switched-on Google plug-ins, with no credentials sent, plus Extra probe targets (`core.probe-extra`, `SKOPAX_PROBE_EXTRA`): up to 20 `name=url` lines.
- Website: Scan after release of (instance field) and `website.release-delay` (minutes, 1 to 120, default 10): one extra page scan after a recorded deploy of a listed service.
- An optional `filter` placeholder on the CSP reports preset, a checked ES|QL condition added after the time condition, and a new `condition` placeholder kind for presets.
- A route's Card tags field takes tag ids or names in any case (`infra`, `Infrastructure`) and offers the tag ids as you type; a term that matches no tag, or the names of several tags, is refused on save with a message naming it.
- Admin > Settings > Card tags shows each tag's id, the empty rules box says "Add rules, such as ...", and the plug-in rules read "Also tagged by plug-ins (fixed, added to yours)".
- On Connect > Integrations, a plug-in that is on but has no instance switched on shows a hollow switch that says to open it and turn an instance on.

**Changed:**

- More cards lean to High: a new WARN log pattern with at least 100 lines in an hour, or one at least 10 times its day or its week baseline with at least 50 lines, is High, and a Prometheus latency card (average, gateway route p95, ingress p95) is High at 3 times its baseline over at least 500 requests.
- On upgrade, Warning cards that the current rules make High become High once: open cards and stopped cards still waiting to be addressed. Resolved, archived and hand-set cards, stopped cards that are taken or muted, and alerts kept at Warning are left alone; Slack posts are redrawn, not posted again. A re-scored card stays High while it is open. See Upgrading to 0.5.9 in the configuration docs.
- High is a brighter orange on the page, the card page, mail, the Slack bar and the tab icon, clearly apart from Warning yellow, with AA text contrast in light and dark.
- Personal notification mails are now on by default, every kind, for everyone who has signed in, once a Mail server is on (they were opt-in). Anyone can switch it off or narrow the kinds in Preferences > Email. Without a Mail server the switch shows off and disabled.
- The batch-jobs example now registers as example-batch-jobs, so it runs next to the built-in Batch jobs.
- The Website page scan job wakes every 5 minutes (was hourly); the scan interval is unchanged. A failed scan shows `error: pages (tried <time>)` and waits one scan interval before the next try, on every pod.
- The Langfuse instance that receives Skopax's own traces counts as healthy; its status line starts with "ok".
- A read tool answer also has Turkish national identity numbers and national-format mobile numbers masked.

**Plug-ins:**

- Cards can carry a known cause (`knownCause`); the card page and the board show it, and the investigator reads it as person-written text.
- `ctx.last_status()` returns the status a check stored for this instance at its last pass.
- `skopax.severity.warn_log` and `skopax.severity.latency` hold the lean rules and their thresholds; Prometheus latency cards carry `requests`.

**Fixed:**

- With several pods, an integration added or changed on another pod shows at once in Admin > Integrations and Connect > Integrations, and Test connection and field suggestions on it use its current values.
- Preferences > Email notices a Mail server set up meanwhile when you open it again, without a reload.
- A background instance refresh that stopped is noticed again by the sweeps even while pages keep reading the integration list.
- The one-time move of open Warning cards to High (0.5.8) no longer skips a card whose hand-set or earlier severity field is stored empty.

## 0.5.8 (2026-11-23)

A High severity, Microsoft 365 mail through Graph, Dengage back in core, a Slack resolve window, an investigator that leans on people's earlier verdicts, and a long list of fixes. Read the upgrade notes under Changed before you upgrade: every pod at once, and warning alerts become High.

**Added:**

- A High severity between Critical and Warning, for problems users feel now: 5xx and error-log spikes, broken pages and ad links, traffic down by more than half, failed deploys, and Langfuse, Umami and Crashlytics drops and spikes. It has its own colour (orange; Warning is now yellow), a filter, a Board chip, a severity change by hand, custom-check and MCP support, and an auto-resolve window that follows Warning unless set (`core.auto-resolve-hours-high`).
- Alert labels map critical to Critical, warning, high, major and error to High, and low and minor to Warning. Alerts kept at Warning (`alerts.warn`, `SKOPAX_ALERTS_WARN`) lists alert-name globs that stay at Warning, for noisy forecast alerts.
- A card whose value doubles moves one level up, once; a severity set by hand is never moved.
- A card resolved as Fixed that fires again comes back as a regression: it is posted at once as at least High, assigned back to the person who fixed it, and says "Came back after fix <ref>" on the page, in Slack and in Teams. A card resolved as No action needed reopens quietly unless it got worse.
- The Mail server can send through Microsoft 365 with Microsoft Graph (Send with: Microsoft 365 (Graph)). It signs in as an app, preferably with a certificate, and sends the same MIME mail through sendMail as one mailbox, scoped by Exchange RBAC for Applications. No SMTP relay or static IP is needed, and national clouds work through Authority and Graph URL. Test connection gets a token and warns, best effort, about a tenant-wide Mail.Send permission. The setup guide lists the Entra and Exchange Online steps. Saved SMTP mail servers are unchanged.
- Dengage is back as a built-in community plug-in. It reads completed push, e-mail and SMS sends and opens cards for large sends, poor delivery against each channel's 4-week baseline, quiet channels and refused channels, and the `dengage` tool lets the investigator read sends and daily sums. The plug-in id, fields, checks and card keys match the earlier extension copy, so saved instances and open cards carry over; the UTC offset now defaults to 0. A plug-in package that registers the id of a built-in is refused with a notice and one warning log line, and the built-in keeps serving.
- The Tools page shows an "Open in Langfuse" link to the project of Skopax's own tracing when Langfuse tracing is on and the project is known.

**Changed:**

- Post immediately from, the Teams and Email lowest severity and the core route default now start at High. A route with card tags and no lowest severity of its own keeps Warning, so business routes still get typo, layout and similar cards; a value you set is kept.
- On upgrade, open Warning cards that are not resolved or archived become High once (`sevBefore` keeps the old value). Nothing is posted and Slack posts are redrawn. Before upgrading, list noisy warning alerts in Alerts kept at Warning, or set Post immediately from to Warning. Upgrade every pod at once (Recreate or one replica): 0.5.8 cannot run beside or be rolled back to an older version once cards are High.
- Resolve in Slack (card menu, post buttons and the Resolve button in a thread) opens a window that asks how, like the page: Fixed, No action needed or Duplicate, an optional why that becomes the card note, and the other card for a duplicate, checked before anything is applied. A No action needed proposal of the investigator opens on No action needed.
- The investigator now treats a person's earlier verdict on the same card (Fixed, No action needed, Duplicate, Known noise, a mute, a ticket they linked, and the reason they wrote) as reviewed ground truth. The card brief lists the last three such verdicts first, the investigator starts from the newest and overturns it only with named current evidence, and auto-resolves, expiries, Jira sweep links, API-token actions, its own earlier notes and earlier answers never outweigh it. The card page leads its "Seen before" line with "Ruled <verdict> by <name>, <date>".
- `past_cards` now answers with short per-occurrence digests (when it fired, how long, how it ended, who decided, the investigator's first note line, and a person's verdict word for word), people's rulings first, and `detail` returns one full occurrence. `past_runs` rows carry a clipped answer, `detail` with a run id gives the whole one, and its answers are now scanned for planted text. past_cards allows 10 calls per run, or 2 per card plus 4 up to 24; past_runs allows 8.
- Langfuse tracing in `metadata` mode sends a short summary as the input and output of each step instead of leaving them empty, which Langfuse showed as "undefined". Model calls show the message count, prompt and system sizes, input tokens and the tools offered, then the stop reason, the tools called and the answer length. Tool steps show the tool and the argument names it declares, then whether it worked and the result length. Traces show the kind and card count, then the outcome and the model and tool call counts. No prompt, answer or tool text is sent, and a tool name or argument the model made up shows as unknown or other.
- A step-by-step guide to tracing Skopax's own AI calls with Langfuse: a separate Langfuse project, a key pair that may write, which instance fields to fill, how to check it and what to do when nothing shows up. It is in the plug-in docs (Langfuse tracing) and in a new "Trace Skopax with Langfuse" section of the Docs page. The help of the Langfuse instance setting says to pick a separate project, because the picked instance is no longer watched.
- The card drawer's Expand and Shrink button sits at the left of its top bar, on the side the drawer grows to, with an arrow pointing the way it moves.
- The esql tool retries a failed or timed-out query of a day or less once on the last hour, and says so; ranges over a day still retry on the last day. Short time units like 3d or 12h are recognised. An unknown index pattern suggests existing patterns that share its start, end or a word.

**Plug-ins:**

- `high` is a severity plug-ins may send (`skopax.contract.SEVERITIES`, `alert_sev`); plug-ins that send only crit and warn need no change.

**Fixed:**

- A Slack reminder for a card assigned by name now mentions the person (through the e-mail saved with the assignment), so they are pinged; before it only printed the name.
- group refuses a bare type prefix such as `log|`; its no-match error says how many of the 5 patterns are used and names open same-service cards the pattern does match.
- slack_update points to report_slack_draft in report runs, and to slack_draft and the run's unposted cards otherwise.
- The card tool points a closed card or an unknown key to open cards with a close key.
- github_file lists the nearest existing directory when a path is not found.
- kube previous_logs falls back to current logs when the previous run left no lines or its logs can no longer be read.
- Email route and notification mail addresses keep the part before the @ as typed and lower-case only the domain, so Alice.Smith@Example.COM is sent to Alice.Smith@example.com. Two spellings that differ only in case are one recipient, and a recipient already marked as mailed in lower case is still recognised.
- A mail the server rejects at the data stage (a 5xx reply) now counts against that one recipient instead of the whole mail server, so the other recipients are still mailed; a 4xx reply is still retried. A partial refusal the server reports after accepting the mail is now also counted as a refused recipient, with only the number of refused recipients in the message.
- The History row's "not posted" count and its tooltip now show in your language.
- Tag errors on the admin page (unknown tag, bad address, too many responsibles) now show in your language.
- A post by a route plug-in Skopax does not know reads "Posted by the <name> route", translated.
- Clicking a link to the card that is already open shows that card again.
- Notifications show "Email not sent" with the reason when the mail server refused the email.
- A refused notification mail is no longer sent again after an outbox error or a long send pass.
- A Jira back-link that keeps failing for the same reason adds one activity line, not one per ticket action.
- Text like UTF-8 or SHA-256 is no longer taken for a Jira ticket when no Jira projects are set.
- Two admins filling in new integrations at the same time no longer block each other's field lists.
- A tag responsible not found in Slack is checked again after an hour instead of a day.
- The thread reason in Slack no longer turns parts of a card name into italics.
- A card posted on a later retry no longer says it was posted at once.
- GitHub rate-limit messages say how long is left, and GraphQL limits wait for GitHub's reset time.
- `?start` or `?end` inside an ES|QL text value no longer counts as the check's time range.
- "1 service" and "1 tool" instead of "1 services" and "1 tools".
- Spanish uses "coste" consistently.

## 0.5.6 (2026-11-23)

Skopax can trace its own AI calls to Langfuse, and admins can see Integrations as everyone else does.

**Added:**

- Langfuse tracing: Skopax can send its own AI calls to a Langfuse project as traces. Each investigator run, chat answer, topic check, self-review and plug-in AI call becomes a trace, with every model call (model, tokens, cost, time, error) and every tool call of the investigator inside it. Switch it on in Admin > Features and pick the Langfuse instance in Admin > Investigator > Langfuse tracing; it is off by default and sends nothing until both are set. Trace content is `metadata` by default, or `full` with the prompts and tool output redacted and cut; a sample setting sends a share of traces, and failed ones are always sent. Recent runs on the Investigator page show Open trace. Traces go out in the background to Langfuse's OpenTelemetry endpoint and never slow down or fail a run; switching tracing off stops sending at once. The Langfuse instance that receives the traces is not watched by the Langfuse check, so Skopax opens no cards about itself. See [Langfuse tracing](docs/plugins.md#langfuse-tracing).
- Connect > Integrations has a View as read-only button for admins. It shows the page as operators and viewers see it, loaded from the same data they get, and Back to editing returns to the editable page.

**Fixed:**

- A test of the investigator's Slack send read the result before the background post finished, and failed now and then on slow machines.

## 0.5.5 (2026-11-23)

Card links you can share, a workspace icon, Jira issues that link back to their card, and a Slack channel you can give by name.

**Added:**

- Opening a card puts it in the address bar (`#c=<number>`, or `#a=<key>` for a card without a number), so the link can be shared and opens that card. Back closes it, Forward opens it again, and Back after a second card returns to the first. Live refreshes add no history entries.
- A workspace icon in Admin > Workspaces: Letter (as before), Company icon (the one uploaded for the single sign-on button) or Upload (an SVG, PNG or WebP up to 64 KiB, checked like the sign-on icon). It shows on the workspace tile in the sidebar, its menu and the collapsed rail; links to other workspaces keep their letters.
- Jira back-links: when a Jira ticket is linked to a card (on the page, over MCP or Slack, by Create task, by the investigator or by the Jira link sweep), the Jira issue gets a link back to the card. Admin > Settings > Jira tasks > Back-links (`jira.backlink`, `JIRA_BACKLINK`) chooses `link` (default, a web link on the issue), `comment` (one short comment) or `off`. The token then needs `write:jira-work` (Link issues, or Add comments); with a read-only token, set it to off. A refused back-link adds a line to the card's activity and is tried again the next time the ticket is set; the ticket stays linked in Skopax either way.
- Linking a Jira ticket to a card is its own notification, "Jira ticket linked - OPS-40 - in progress", from every path that links one, the Jira link sweep included. Before, a ticket linked while the card moved to In progress only read "In progress".
- Admin > Settings > Slack > Channel takes a channel name like `#alerts`. It is looked up in Slack (this needs `channels:read`, and `groups:read` for a private channel), saved as the channel id so a later rename does not break it, and the page shows "Posts to #alerts" under the id.

**Changed:**

- "Action taken" is called "Done": the card filter, the list heading, the filter tooltip and the Services status.
- The Slack setting "Post at once from" is called "Post immediately from".
- The new-version dot in the sidebar has a tooltip with the new version and the one the server runs; the Admin row and the user menu show the same text.

**Fixed:**

- A long Slack status tag on a card row, such as "Not posted: the run read suspicious text", ends in an ellipsis instead of being cut at the column edge.
- On Admin > Access, a row note such as "Role set to admin" sits under the row menu instead of wrapping beside it, and a note that says a change saved goes away after 5 seconds.

## 0.5.4 (2026-11-23)

A Docs page that helps, and a calmer top bar.

**Changed:**

- The Docs page (More > Docs) is a guide you can search: Get started, A card's life, Card actions, The investigator, Notifications and messages, Links and keyboard, For admins (with links to the full docs) and a short glossary. Each section folds open, and the search box filters them as you type. Where the page knows your settings it shows them, such as auto-resolve hours for admins and the investigator's mode and daily limit for everyone, and it says plainly when the investigator cannot run and why.
- "Last check" and its green dot left the top bar, together with the "Updated" flash after a refresh. The time of the last check is in the tooltip of each source pill on Overview.

**Fixed:**

- The Docs page said held Slack posts were under Notifications; they are on the Activity page since 0.5.1.

## 0.5.3 (2026-11-22)

Send a real test mail through the Mail server, a wider card drawer, proper dialogs for passwords and names on Admin > Access with a password generator, and an Untagged quick filter.

**Added:**

- Send test mail on the Mail server integration: one real mail to the admin who presses it, through that instance's settings, with the server's own answer (accepted, or the refusal and its reason). Only to the signed-in person's own address, at most 5 an hour, and each try is on the Activity page without the address. A green Test connection alone does not prove delivery; the setup steps now end with Send test mail.
- Expand and Shrink on the card drawer: the drawer widens to 1400 px for a bigger view, and the choice is remembered in this browser. Hidden on narrow screens.
- A Generate button for passwords in Reset password, Set a password and Add person: 20 random characters from the browser's secure random source, without look-alike characters, with Copy.
- Untagged in the Tags group of the board's quick filters, as in the sidebar.

**Changed:**

- Reset password, Set a password and Edit name on Admin > Access open a dialog with a labelled field, the rules and the server's answer, instead of a small form in the row. Change role left the row menu; the role list in the table changes roles.

**Fixed:**

- A mail server refusal that repeats a recipient address no longer writes the full address to the log; it is masked as a***@example.com for every mail Skopax sends.

## 0.5.2 (2026-11-22)

Two fixes: a race that could crash an investigator run, and held Slack posts that stand out again.

**Fixed:**

- Two investigator runs started at the same moment could crash one of them ("threads can only be started once"), and a run thread that had not started yet could be left out of the count of running investigations. Each run thread is now started inside the lock.
- While Slack posts are held, the Activity item in the menu turns to the warning colour, as the Notifications item did before 0.5.1, and it stays visible when its menu group is folded. It returns to normal once the posts are sent or deleted.

## 0.5.1 (2026-11-22)

Clearer Integrations and Notifications pages: integrations get icons instead of letters, Kubernetes agent instances show their health, the email integrations read as two different things, and the Notifications page lists only notifications.

**Upgrade notes:**

- The Email plug-in is now named Email route; its id `email`, its stored instances and its settings keys are unchanged. Its setting labels read Email route repeat days, Email route lowest severity and Email route fixed mails.
- The Not posted to Slack list and the Slack posts of the last 24 hours moved from the Notifications page to the Activity page, and the "N not posted" count moved with them.

**Changed:**

- The Notifications page lists only your notifications, and says so when there are none.
- Built-in and example integrations show a simple picture (Lucide icons, ISC) on their tile instead of two letters. No brand logos are used.
- Email route and Mail server have descriptions that say what each one is: the Mail server is the connection to your SMTP server, set up once; an Email route mails chosen cards to a list, like a Teams route.

**Fixed:**

- Kubernetes agent instances on Connect > Integrations show their own health from the primary's heartbeat: Healthy with its age, a warning, or the error. After a failed heartbeat they show when the agent was last seen instead of a fresh time.
- Two page checks could fail on a millisecond tick (board tiles and stopped labels of event cards).

## 0.5.0 (2026-11-22)

Email: a Mail server integration, Email routes that mail cards to a list, and personal notification mail, off until each person turns it on. The Assigned note in a card's Slack thread mentions the assignee, and the investigator's own websites come from a setting of their own instead of sign-in email domains.

**Upgrade notes:**

- A plug-in of your own whose `core` range ends at `<0.5` is listed as incompatible and does not load on 0.5.0. Widen it to `<0.6`; the plug-in contract only gained optional fields, so nothing else needs to change. The built-in and example plug-ins now declare `<0.6`.
- Sign-in email domains no longer count as your websites. Set Your websites in Admin > Investigator > Web (`core.own-sites`, `SKOPAX_OWN_SITES`), or rely on the sites of your Website integrations, which are used when it is empty. Sign-in is not affected. A value with a scheme, path or port in `SKOPAX_OWN_SITES` stops the start with a message that names it.
- Nothing is mailed until an admin adds a Mail server integration. Personal notification mail stays off for every person until they turn it on.

**Added:**

- Mail server plug-in (Connect > Integrations): sends email through your SMTP server with STARTTLS, TLS, or plain text to a private address only. Test connection signs in and says in plain words what failed, without sending a mail. One instance per install. See [Set up email](docs/setup.md#set-up-email), which also covers Microsoft 365.
- A shared mail sender for core and plug-ins (`skopax.mail`): one layout for card mails in plain text and HTML that reads the same in light and dark mail clients, in the outbound language, with auto-generated headers and header-injection and address checks.
- Email plug-in: each instance is a route that mails the cards it takes to a list of up to 50 addresses, one mail per address, with the same card tags, card keys, lowest severity, repeat days, retries and Slack echo as Teams routes. A refused sign-in at the mail server is tried again later instead of costing one sign-in per address. The card page says "Sent by email" for these posts.
- Personal notification mail: each person can turn on "Email me my notifications" in Preferences > Email and pick the kinds. Mail goes only to the address of their own account, at most 20 an hour and then one summary, is tried again for up to 24 hours while the mail server is down, and is sent once across pods.
- Your websites (`core.own-sites`, `SKOPAX_OWN_SITES`) in Admin > Investigator > Web: the sites of your company the investigator may read and search, as host names. The row says which source is in use.

**Changed:**

- The Assigned note in a card's Slack thread mentions the assignee, by their Slack id or by name when Slack has no match; the person who assigned stays plain text.
- Route plug-ins may name a target that is not a link (`Outbound.target_field`), say when they cannot post yet (`Outbound.ready`), and answer a `reason`, `retry: False` and `progress: True`. A refused post is then given up at once instead of after six tries, and the notice says how many tries were made.

## 0.4.12 (2026-11-22)

The 0.4.11 changes, published as an image. 0.4.11 was tagged but its image was never published: the release scan found two High findings in libc6 from the base image.

**Upgrade notes:**

- Coming from 0.4.10, read the 0.4.11 notes below: they all apply.
- The image still carries libc6 2.41-12+deb13u4 from the distroless base, which has two High findings (CVE-2026-19499, CVE-2026-5435) fixed in Debian 2.41-12+deb13u5. They are listed in .grype.yaml until 2026-12-06; the next release bumps the base once distroless ships the fix.

## 0.4.11 (2026-11-22)

Tagged but not published as an image; these changes ship in 0.4.12.

A cleanup release: the investigator is called Investigator everywhere, a password change signs out your other sessions, Slack echo posts say which card they are, grouped cards name their parent, and many small fixes across Slack, the board, Admin and the plug-ins. CI runs the page checks in parallel.

**Upgrade notes:**

- The investigator is named "Investigator" (translated with the outbound language) on the page, in Slack and in history, whatever model it runs on. Before, a Claude model showed as "Claude investigator".
- Resetting or changing a password ends that person's other password sessions. The session that changed its own password stays signed in; SSO sessions and API tokens are not touched. Sessions from before the upgrade keep working until the next password change.
- `SKOPAX_ENV_LABEL` (setting key `core.env-label`) is now `SKOPAX_WORKSPACE_LABEL` (`core.workspace-label`). The old name still works and logs a deprecation line once; the new name wins when both are set. `/api/me` returns `workspaceLabel` instead of `envLabel`.

**Changed:**

- Notes, activity and history lines of a grouped card name the parent as "#N service / label" instead of its internal key (the key is kept only when the parent has left the page).
- A card post placed in another card's Slack thread says why it is there ("Grouped under #N ...", "Same service as ...", "Same namespace as ..."), and an echo line in a shared thread starts with the card's own linked name.
- SSO letter marks use two letters where providers shared a letter (Google, GitLab, Okta, OneLogin, Auth0, authentik).
- Admin > Web sites warns when a -host names a site that is not on the built-in list.
- CI runs the page checks as eight parallel jobs (two time zones, four shards each) behind the same required pages check.
- Docs: one "Set it up" format in the plug-in guide, the SonarQube gate source, the page-scan cut-off wording, and the order in which core and plug-in page-text rules apply.

**Fixed:**

- Slack reminders @mention the assignee again after a failed Slack lookup: a failure in the daily tag report no longer stops mentions outside that report, a refused lookup is tried again after 10 minutes, and while Slack cannot be asked the last known user id is used.
- Slack name lookups no longer repeat every pass when Slack refuses them (for example a missing users:read.email scope); a refusal is held for 10 minutes and logged once.
- The daily tag report is tried again later the same day when Slack clearly never got the post (connection refused, name lookup failed, rate limited); when Slack may have taken it, the day is skipped as before, so it is never posted twice.
- Route posts retried in the same pass as new pushes are all counted and echoed to Slack; a plug-in digest post marks its cards while it goes out, so a route echo cannot post them a second time.
- A Slack post is never written in the old language right after the language packs load.
- When the Slack redraw lease cannot be read, the card-post check skips that pass and logs it.
- A post-at-once card keeps its "Card posted to Slack at once" activity line when the auto-send worker sends it first. Admin > Tags reads the people list once.
- Board: dragging follows the same busy rule as the tile buttons, and a busy card refuses a drop.
- Information: muted information can be unmuted; Move to Information on a card someone works on says it stays on the board until then; History no longer shows past closed rows of a key that is information now.
- History: failed and cancelled actions are translated, "Assigned to name" has no colon, and "Before that" lines read in sentence case in English.
- Releases names the code host that was not read (GitHub, GitLab or "the code host") instead of always GitLab; deploy notes, code-link errors and the Enricher log do the same.
- The self-review texts translate again in de, es, fr and tr.
- The Name and Change password dialogs keep keyboard focus inside; Esc closes them and focus returns.
- Admin > Access locks Change role and Disable for the last enabled admin with a password, with the reason as tooltip.
- A database error while saving the identity provider's display name no longer refuses an SSO sign-in. A password set on an SSO account is hashed once. SSO link and SSO-only refusals name their field.
- The people search text no longer shows in HTTP access logs. Saving a setting at the value already in use adds no activity line. Access row menus stay open while the page scrolls.
- Confluence with no Spaces shows "Knowledge needs setup", and an MCP server error affects the Knowledge badge only when that server is a knowledge source.
- GitHub: an unknown repository name is remembered for a minute, and github_docs reads a file once.
- ES|QL reads of daily indices named with the local date retry the day before around local midnight.
- Actions recorded under a display name two people share, or made with a token, no longer count as page use.

## 0.4.10 (2026-11-21)

Calmer board tiles and a searchable Group picker, assigned cards that wait for their person, Slack posts that carry the investigator's summary and safe links, a fix for a save that could get stuck, and failure toasts on every failed action.

**Upgrade notes:**

- A card assigned to someone (by hand or by auto-assign) that stopped firing now waits three times its severity window before it is auto-resolved (18 hours with the default of 6, counted in business hours when Business days only is on), counted from the latest of when it cleared, was assigned or was reopened. Unassigned cards keep one window; plug-in windows still skip assigned cards. The assignee gets an Auto-resolved notification that says why.
- A card's Slack main post that went out before the investigation (a route echo, or the server's numbers-only post) is updated in place once the investigator has a result: a bold headline, a short summary, "Recommended: ..." and "Notified: <routes> (<channel>)". The full findings stay in the thread, nothing new is posted, and a post with the investigator's or a person's own text is never changed. Route posts recorded before 0.4.10 count as delivered once they are 10 minutes old.
- A note or recommendation written by an investigator run that read planted (untrusted) text is marked on the card and never used for the main post until a clean run writes it again.
- Route post records now mark a delivery only after it went out.

**Added:**

- Group: the parent picker is a searchable list. Before typing it suggests cards on the same service, then the same namespace or cluster, then cards that opened around the same time; you can search by number, service, label, namespace, tag, ticket or owner.
- A red "Not done: <reason>" toast whenever a save or a card action fails.

**Changed:**

- Board tiles show the short stopped tag ("Stopped firing 21 Nov 19:39") in the stopped colour instead of critical red; the full wording with the assignee is in its tooltip, the card drawer and the card page.
- A card with no service is named by its namespace, subject, cluster instance or alert everywhere (board, card page, palette, Group picker, closed list, history, costs, drafts), never "-".
- Auto-resolve notifications say why, translated with the page language.
- Investigator text in Slack: an address meant as a link is written in full with https; a shortened address is written as a path. Skopax keeps shortened addresses from becoming half links, turns bare addresses of your own sites into https links, and leaves addresses with a port, internal names and text people type as written.
- "Asks" are called requests on the page (Page requests, Slack requests).

**Fixed:**

- Saving a setting (for example Daily limit) could get stuck with a page error when another row had a field without a class.
- Board tags and the tile footer wrap inside the tile instead of overflowing into the next column.
- A grouped card that follows its parent into In progress sends an In progress notification instead of Auto-resolved.

## 0.4.9 (2026-11-21)

A Slack post of a stopped card says its status once, and the daily tag report shows in-progress counts and the open cards nobody is responsible for.

**Upgrade notes:**

- Slack posts of cards that stopped firing are redrawn once after the upgrade, so their facts bar loses the repeated status. Other posts are not touched. Nothing new is posted.
- The daily tag report now also posts on a day when the only open cards are ones nobody is responsible for (untagged, or tagged only with tags that have no responsibles), as a closing line without mentions.

**Changed:**

- Slack: for a card that stopped firing, the head keeps the full status with its ending ("stopped firing - awaiting action - assigned to Alice") and the facts bar shows only the short state ("Stopped firing" or "Stopped after the event") before severity and time.
- Daily tag report: each tag line also says how many of its cards are in progress (taken), for example "3 open cards: 1 critical - 2 in progress"; a tag whose cards are all in progress is listed with that count and without mentioning its responsibles. The cards listed under a tag stay the open ones.

**Added:**

- Daily tag report: a closing line counts the open cards nobody is responsible for, with up to 3 of them (worst first) and a link to the Open view, without mentions. The plain-text version always keeps this line.

## 0.4.8 (2026-11-21)

Slack card posts always match the card, a stopped card says who has it, and clearer investigator tool errors from the self-reviews.

**Upgrade notes:**

- Every tracked Slack post is redrawn once after the upgrade (draw version 3). Nothing new is posted.
- A card that stopped firing ends in one of three ways, in the Slack head, its facts bar and on the page: "stopped firing - unassigned" (Turkish "sinyal durdu - atama bekleniyor"), "stopped firing - awaiting action - assigned to Alice" ("aksiyon bekleniyor - atanan: Alice"), or "stopped firing - Alice is on it" once someone took it ("Alice ilgileniyor"). The "after the event" forms take the same endings. Muted, resolved and information cards keep the short form. "Awaiting review" is gone from posts, thread notes, reminders and Teams route posts.
- Names are display names (the people list, then Slack), never e-mail addresses. Cards store the owner's display name when someone takes the card, creates a task or a Jira ticket sets the owner.

**Fixed:**

- A Slack post no longer keeps a head that no longer fits the card, such as "Skopax resolved #146" on a card that was reopened and is in progress. Every rewrite checks the head; a fresh head that does not fit falls back to the stored one when that still fits (so "alice resolved" stays), else to the head for the card's state.
- A take written by the system names the owner instead of the product.
- A failed Slack name lookup while drawing posts no longer stops later lookups.
- Business route posts no longer say or imply that someone is looking into or fixing the problem: the investigator's text says what is happening for users and, when confirmed, whether the cause is ours, an outside provider's or not known yet. When the confirmed cause is at an outside provider and nothing of ours needs a change, the owning business team gets an action to contact the provider, with what to ask about.

**Changed (investigator tools):**

- An instance name of another tool offered in the run (such as a Kubernetes cluster given to promql) is refused with the names the tool takes and, when one name is close, "did you mean". The instance field says which names each tool takes.
- esql: an unknown column lists up to 5 close columns. When today's daily index does not exist yet but its pattern does, the query is read once for the day before, and the answer says so; a mistyped index keeps the closest-pattern hint.
- group: a refused parent lists the parents this run can use, and an unknown child is named.
- card: a closed card answers by key or #number with its outcome, when it was cleared, and the open card it was merged into.
- slack_update is offered only when a run card, its open parent, or a same-service twin (open, or posted in the last 6 h) has a main-channel post.
- crashes: an app that is not set up is refused with the apps this install reads.
- qan: a missing-column error names the column and up to 5 close existing columns, so the query can be rewritten.

## 0.4.7 (2026-11-21)

The page uses the same words as Slack for cards that stopped firing, Slack posts follow look and language changes by themselves, refused Slack users are told how to get access, and fixes from the 0.4.6 reviews.

**Upgrade notes:**

- Each tracked Slack post is redrawn once after the upgrade from 0.4.6, because posts now remember the draw version and language they were drawn with. Later look or language changes redraw them the same way. Nothing new is posted. The upkeep waits for the language packs, at most 2 minutes after start, and does not run while a manual redraw holds its lease.
- The page calls a card that stopped firing what Slack calls it: "Stopped firing - awaiting review" (Turkish "Sinyal durdu - inceleme bekleniyor") while it waits for someone, and "Stopped firing" with the time when it is muted, taken, resolved or an information item.
- Web sites for the investigator (`investigate.web-domains`) are now checked with the same host rule the investigator uses: wildcards (`*.example.com`) and one-word names (`localhost`) are refused when saved. `any`, `off`, `none`, `false` and `no` must stand alone.

**Added:**

- A Slack user whose card action is refused is told the way in: sign in once (then ask an admin when new sign-ins get the viewer role), ask an admin to add them when only added people may sign in, ask an admin for operator access, or ask an admin for access when roles come from a role map or the sign-in provider. A disabled account or an e-mail domain that is not allowed gets the plain refusal.
- A plug-in ReadError can ask for up to 800 characters of room for its message (default 300); docs/extending.md documents both.
- Web sites in Admin use the chip editor, including `-host` chips.

**Changed:**

- Auto-sent Slack posts and the page's draft line name the investigator as elsewhere ("Claude investigator - auto-sent" when a Claude model runs it).
- The hours and days of auto-close windows and "Takes" are translated in every pack.
- Search: ticket suggestions always keep the "Open in Jira" row for the best match, clicking inside the top search field places the caret, and the dropdown follows a window resize.
- MCP card actions are stored once with their source.

**Fixed:**

- Old history rows of the workspaces setting show "Other workspaces"; an interrupted rename records its history line, written once, before the old setting is dropped.
- The tag colours warning is logged at most once a minute while the database is down.
- Two Slack tests that failed now and then are stable.

## 0.4.6 (2026-11-21)

A clearer mark for cards that stopped firing, old Slack posts redrawn, the headless browser as its own plug-in, a search box in the top bar with ticket completion, a notification bell, Admin > Workspaces and list editors for comma settings.

**Upgrade notes:**

- Slack and Teams: a card that stopped firing but is still open now shows :large_yellow_circle: "stopped firing - awaiting review" (Turkish "Sinyal durdu - inceleme bekleniyor") instead of :eyes: "stopped firing - still open". The check mark still appears only when the card is closed.
- On the first start of 0.4.6, Skopax redraws its existing Slack card posts once in the background with today's head, marks and words (open cards first, at most 500, throttled to Slack's rate). It only edits posts it made; it never posts new messages or touches thread replies. Admins can run it again from Admin > Integrations > Slack > "Redraw Slack posts". One run at a time holds a lease in the database, also across several server copies.
- The headless browser is now a plug-in of its own, "Headless browser" (Connect > Integrations), with its health on its card; it owns the browser-down and access cards (same card keys). A Website instance picks a browser instance; when exactly one exists it is picked by default. The older `browser-url` field on a Website instance keeps working and wins when set, and Admin offers a one-click move of it into a Headless browser instance.
- Web sites for the investigator (`investigate.web-domains`) now add to the built-in list instead of replacing it; the built-in sites are shown, your own e-mail domains are included, and `-host` removes one. The list is capped at 100 hosts.
- About your company (`core.org-about`) is now read by every investigator prompt (chat, Slack questions, card investigations, self-review), framed as background written by the admins; its limit is 2000 characters.
- The other deployments listed in the sidebar switcher are now called workspaces: the setting is `core.workspace-links` / `SKOPAX_WORKSPACE_LINKS` and has its own Admin section.
- An out-of-range chat tool-call cap or Slack question tool-call cap in the environment or configuration now clamps to the nearest bound (3 to 30) with a warning, instead of falling back to the default.
- Renamed in the page: the user page "Settings" is now "Preferences", Admin "General" is now "Settings", and "Phone push" is now "Slack DMs for critical cards". Addresses (#t=...) are unchanged.

**Added:**

- A search box in the top bar (Cmd/Ctrl K) that opens the palette as a dropdown under it; on phones a search icon opens it. Typing part of a ticket key or its digits suggests the full key; Tab completes it, Enter opens the card, and "Open KEY in Jira" opens the ticket.
- A notification bell in the top bar with the unread count; it opens Notifications.
- Admin > Workspaces: this workspace's name and the other workspaces as rows (name, address, Open, Remove, move up and down, "+ Add workspace"), with checks per row and one Save.
- List settings (MCP allowed clients, self-review recipients, Jira projects, Slack DM people, Slack question channels, certificates not watched, SSO scope) are edited as chips: Enter, comma or paste splits, each entry is checked, Backspace removes the last. The server checks each entry with the same rules. The saved text is unchanged.
- Board: when a filter is on, "All" and "Clear filters" sit next to "Showing ..."; Escape on the Board clears every filter.
- Chat: separate effort and tool-call settings (`chat.effort`, `chat.max-tool-calls`); unset follows the Slack values.
- Nodes sits under Connect after Integrations; admins can edit a node or add one from the Nodes page.
- The Slack reminder mentions the assigned person.
- The Compose example and docs include the browser service; docs/plugins.md has a Headless browser section and a Deploys section.

**Changed:**

- Reopening a card drops the note the system wrote when it closed it, and tells the Slack thread "X reopened #N" when the card was really closed.
- Board columns fill the board height, so a card can be dropped anywhere in a column.
- Inside Skopax the chat hides its own theme toggle and avatar menu; the chat in its own tab keeps them.
- The triage pill on a card no longer cuts its label off.

**Fixed:**

- The folded Activity group no longer shows a count for Slack drafts that is hidden when it is opened; it shows the unread notifications.
- A Slack reminder never shows an assignee's e-mail address.

## 0.4.5 (2026-11-21)

A card that stopped firing never looks closed, the investigator is named as such, clearer investigator tools, and the single sign-on page shows whether MCP is connected and who uses it.

**Upgrade notes:**

- Slack and Teams: a card that stopped firing but is still open now shows :eyes: "stopped firing - still open" instead of :white_check_mark: / "cleared" / "back to normal". The check mark appears only when the card is closed: resolved by a person, closed by the auto-resolve timer (the post says so, e.g. "closed automatically: stopped firing for 6 business hours"), or marked known noise (known noise now shows the check mark instead of the severity circle).
- The investigator is shown as "Claude investigator" when a Claude model runs it, else "Investigator", everywhere people see who did something (Slack, card history, notes, costs, MCP), instead of "Claude (pod)". Stored values are unchanged.
- MCP use is recorded separately from page sign-ins: an MCP sign-in sets "last sign-in" only for a person who has none. The first start builds a new index on actions (requested by, time).
- People who used MCP before this release are labelled once from what the server already has: page actions or page chat questions give "Page and MCP", only MCP-made actions give "MCP only", and no evidence gives "Uses MCP" until their next page sign-in.
- A plug-in tool called with an instance name that is not configured (for example a cluster this install does not watch) is refused with the valid names and a warning not to use another one in its place.

**Added:**

- Admin > Access & sign-in > Single sign-on: an MCP status line (connected with people in the last 7 days and the last sign-in, set up with no one connected yet, or not set up with the missing piece) and the MCP setup values, marked when they are built-in defaults.
- The People list marks "MCP only", "Page and MCP" or "Uses MCP", with the last MCP use.

**Changed:**

- Investigator tools answer more helpfully: group names the patterns given, the ones held and the slots left; esql suggests the closest existing index patterns for an unknown index and retries once on the last day when Elasticsearch cannot serve the whole range (and says so); gitlab_file and gitlab_blame list the nearest existing folder for a wrong path; kube previous_logs falls back to the current log, marked as such, for a container that never restarted; slack_update on a card with no Slack post says what to do instead.

**Fixed:**

- The "back to normal" thread notes and reminders no longer suggest a card is fixed while it is still open.

## 0.4.4 (2026-11-20)

Slack posts that go out directly, a History tab on every card, soft tag colours, a toast when a change hides a card from your filter, and an MCP server that says which version it runs.

**Upgrade notes:**

- Slack posts go out directly. When Slack auto-send is on (Admin > Features), the investigator posts its card and deploy-report messages to Slack at once. There are no drafts, no countdown, no Hold and no Slack recommended tag, filter or chip any more. With auto-send off, nothing posts on its own: the investigator's post is dropped with a note on the card, and the server's own posts and deploy-report texts are not queued; Send to Slack still posts by hand.
- A run that read planted text does not post; the card shows the text with Send to Slack. Such kept text never holds back the server's own posts for that card or its service.
- A post that still fails after 3 tries 10 minutes apart says so on the card, and the Slack tab lists it.
- On the first start after the upgrade, waiting drafts younger than one hour on cards that still qualify are sent once (or noted as skipped when auto-send is off); other waiting, held or person-edited drafts are cleared. Later restarts keep queued posts.
- The draft-edit and hold API endpoints answer 410 for people; a token's post text is checked like the investigator's and refused with 400. Old #t=anoms/slack links open the default list.
- The daily tag report posts only in the two hours after Daily report at, on business days; a server started later skips that day instead of posting late.
- The first start builds two new database indexes (activity by card key and time, actions by target and time); on a large install this can take a moment.

**Added:**

- A History tab on the card (drawer, expanded card and the card page): who changed what and when, newest first, with old -> new values, assignments, tags, severity changes, Slack and Teams posts, investigation runs and tickets.
- A toast when a change you make hides a card from the current filter, with View card and Clear filter.
- Soft tag colours on cards, board tiles, filters and Admin, with an optional colour per tag in Admin > General.
- The MCP server says what it is: its instructions open with "<Name>, powered by Skopax <version>, extended by <package>" (only the name when the attribution is hidden), and a new read-only about tool lists the version, edition, extending packages and every plug-in with its state (admins also see why one is not on).
- Admin > Changes lists people and access changes by name.

**Changed:**

- The business note has its own row under Tags: "Write note for <team>" or "Edit note"; without a business tag a short hint shows instead.
- Code rows show the file name on top (shortened, full name on hover) and the explanation under it.
- A re-fired card reads "Reopened HH:MM - fired again", then "Before that: ...", without repeating the same event.
- Responsibles without a Skopax account now get their line in the tag report; only disabled users are left out. Changes show responsibles by name, also for older rows.
- The Docker Hub page now shows the overview of the current release; it is updated with every release.
- About your company (Admin > Investigator) is a four-line text box instead of a single line.
- Uploading a company icon for single sign-on switches the button icon to Company.

**Fixed:**

- Long Slack texts, links and investigation text wrap inside the card instead of sliding under the side column.

## 0.4.3 (2026-11-20)

A running investigation you can see at a glance, investigator status in the top bar, responsible autocomplete with auto-assign by tag, and a calmer, consistent settings design.

**Upgrade notes:**

- Auto-assign is on by default for every tag that has responsibles: a new critical or warning card that gets such a tag, with nobody on it, is assigned to one of the tag's responsibles in turn (people with a Skopax account only). Switch it off per tag in Admin > General. Cards open before the upgrade, cards a person has assigned, taken or unassigned, muted cards and grouped cards are never auto-assigned.
- The responsible picker can suggest Slack workspace members; listing them needs the Slack scope `users:read` (already implied by `users:read.email`). Without it the picker offers Skopax people only.
- On a phone the top bar shows "STOPPED" and an icon-only investigator spend button when the investigator is stopped.

**Added:**

- A run banner under the card header while an investigation is queued or running: elapsed time, tool calls, cost so far and the last tool, then the result ("done" or "failed" with the reason). The Investigating tag and board tiles carry a live dot, and the card's cost line includes the running cost.
- An investigator pill in the top bar on every page: the card or cards being investigated, the queue, and when the investigator is paused, over its daily AI cap, or missing a key or model.
- Responsible autocomplete: type any part of a name or e-mail to pick from Skopax users, people responsible on other tags, and Slack members.
- Auto-assign new cards by tag, in turn among the tag's responsibles, with the usual assignment notification and an "Auto-assigned to ... (tag ...)" line in History.
- One settings design across Admin, Preferences and Integrations: titled section cards, label and help beside each control with Show more for long help, quiet source tags, segmented choices, a single Save per key, a wider add-integration list with whole badges and two-line descriptions, a switch for an instance's On, and 44 px targets on phones.

**Fixed:**

- The card page no longer waits for the investigator status before it shows.
- A Slack rate limit is retried later instead of dropping card post updates.

## 0.4.2 (2026-11-20)

Info findings become information instead of cards, SSO-only people, a clearer Single sign-on admin page, any e-mail address as a tag responsible, and jsdom 31 for the page tests.

**Upgrade notes:**

- Info findings are no longer cards. They leave the board, Slack, Teams, reminders, the daily tag report, the investigator and the MCP card tools, and show on a new read-only Information page (Observe > Information). An info finding someone owns, is assigned, has a linked ticket or is working on stays a card until it is closed. Card numbers are given to cards only.
- Info business sentences (for example website typos, VWO results, Google Ads redirects) no longer go to Teams business routes.
- `core.info-auto-close-days` (`SKOPAX_INFO_AUTO_CLOSE_DAYS`) is deprecated and ignored; information items end when their signal stops and leave the Information page a day later.
- `info` is no longer a choice for `slack.post-sev` or a Teams route's minimum severity; a stored `info` is read as `warn` and logged once.
- Tag responsibles may be any e-mail address, not only people with a Skopax account. A removed Skopax user is no longer marked on a tag; a disabled one still is.
- The Single sign-on status reads Needs attention (was Needs setup), and it also shows when single sign-on is on but the provider cannot be reached.

**Added:**

- Information page with what is still firing first; Change severity can move a card to Information, and a hand-moved item can go back to the board.
- Make SSO only for people with a password while single sign-on is on: the password, its sessions and the API tokens made with it stop working, the next SSO sign-in needs a verified email, and an enabled admin with a password always remains. Any SSO-only person can be given a password again.
- Tag responsibles by e-mail address with suggestions; each new address is checked in Slack and marked when Slack does not know it.
- Single sign-on admin page: a status header, numbered setup cards with a copyable redirect URI and a live sign-in button preview, one save bar for unsaved changes, and a phone layout.

**Fixed:**

- The last enabled admin with a password can no longer be disabled or demoted.
- Page tests run on jsdom 31.

## 0.4.1 (2026-11-20)

Tag responsibles with a daily Slack report, business-day timers, a phone layout, Knowledge badges in Integrations, and an AI action group on cards.

**Upgrade notes:**

- New Admin switch Cards > Business days only (`core.business-days`, `SKOPAX_BUSINESS_DAYS`) is on by default: weekend time (Saturday and Sunday in `core.timezone`) no longer counts toward auto-resolve windows or info close days, so a card that stopped firing on Friday evening closes on Monday. Auto-resolve notes say "business hours" or "business days". Card posts to Slack and Teams still go out on any day. Set it to `false` for the old wall-clock timing.
- The daily tag report and the weekend rule both follow `core.timezone` (default UTC). Set it to your local zone so the report arrives at the time you expect.
- Tag report mentions need the Slack scope `users:read.email` (already in the example app manifest); without it names are shown in plain text and Admin says so.
- Connect > Integrations has no Company knowledge group any more: each plug-in is one tile in its own group, and knowledge sources carry a Knowledge badge.
- `GET /api/admin/tags` is admin-only.

**Added:**

- Tag responsibles: admins pick people per tag in Admin > General.
- A daily Slack tag report (`tags.report-at`, default 10:00, Monday to Friday, empty turns it off): one line per tag with open cards, mentioning its responsibles, with the critical and warning split, a link to the board filtered to that tag, up to three linked cards and "+N more". Posted once per day across server copies.
- Knowledge badges (Knowledge, Knowledge off, Knowledge needs setup) on knowledge plug-ins and MCP connections, with the Knowledge chip listing exactly them.
- A "+ Add custom MCP" button next to "+ Add integration".
- Chat and Investigate as an AI group on the card page and in the drawer, styled like Status.
- Phone layout: one board column at a time with column tabs, a compact board header, a full-screen card drawer, larger tap targets, no zoom on inputs, safe-area insets; short landscape phones get it too.
- The Tools page shows Held and Refused input as their own columns.

**Fixed:**

- Saving a severity at its current value no longer writes an empty History row or pins the other severity.
- A board card cannot be dragged while one of its actions is still running.
- Tests that depended on today's date (screenshot retention, spend backfill) no longer fail as time passes.

## 0.4.0 (2026-11-20)

OpenAI as an AI provider, Admin settings grouped into cards, Integrations under Connect and read-only for non-admins, per-severity auto-resolve, clearer card actions on the card page, the drawer and board tiles, and a board that opens on Open.

**Upgrade notes:**

- Prompt cache writes are now counted in AI spend (Costs page and the daily cap). The bill does not change, but the shown spend for the same work is a little higher, so the daily cap is reached a little sooner.
- `SKOPAX_AUTO_RESOLVE_HOURS` (`core.auto-resolve-hours`) is deprecated and hidden in Admin. It still seeds the new `core.auto-resolve-hours-crit` and `core.auto-resolve-hours-warn` while they are not set, and an old value of 0 keeps info auto-close off too.
- Integrations moved from Admin to Connect > Integrations in the sidebar. Admins edit there as before; everyone else sees a read-only view with secrets masked.
- The board now opens on the Open filter instead of All, and the way back reads "Back to Open".
- Plug-ins built for 0.3 keep working; the example plug-ins now declare `core` as `<0.5`.

**Added:**

- OpenAI as an AI provider, one provider per install (`SKOPAX_AI_PROVIDER`, `OPENAI_API_KEY`, Admin > Investigator > Provider). It uses the Responses API with no data stored at OpenAI. OpenAI has no fallback model and no web page reads.
- Model and effort are picked side by side in Admin.
- Admin General and Investigator settings are grouped into titled cards with units and source tags.
- Auto-resolve hours per severity (Critical, Warning) and close-after days for info cards still firing (`SKOPAX_INFO_AUTO_CLOSE_DAYS`, default 3), all editable in Admin.
- A read-only Integrations view for non-admins, and a `knowledge_also` plug-in option that shows when a plug-in such as Jira also feeds company knowledge.
- Card actions in labelled groups (Status, Owner, Links) with icons on the card page and in the drawer, and a quiet action row on board tiles.
- The Tools page lists guard holds and refused tool input apart from real errors.

**Fixed:**

- The planted-text guard no longer fires on Skopax's own words, such as the default "known noise" reason, while text a person or a source wrote is still checked.
- The ES|QL tool points the investigator at the configured index pattern when a dated index does not exist yet (daily index names follow UTC).
- The Preferences language list offers every loaded language pack by its own name, not only Auto and English.

## 0.3.0 (2026-11-19)

Custom checks that read Elasticsearch with ES|QL and a CSP reports preset, built-in language packs, Slack posts from a severity you choose, a batch jobs example plug-in, and sharper investigator prompts.

**Upgrade notes:**

- Warning cards now post to the main Slack channel at once by default, like critical cards. Set Post at once from (Admin > General > Slack) to Critical for the old behaviour. A stored or environment `slack.crit-now` / `SLACK_CRIT_NOW` that is not `true` still turns posting at once off until Post at once from is set.
- With Language on Auto (the default), people whose browser prefers Turkish, German, Spanish or French now see the page and the chat in it; English stays one click away in Settings > Appearance.
- A Slack reply to one person (an off-topic answer, a small-talk or ask limit notice) now follows that person's language when a built-in pack covers it. Channel posts, tickets and the card language stay English until an admin picks another.
- Deprecated: the CSP reports plug-in. Use the CSP reports preset under custom checks instead; the plug-in leaves in 0.4.
- Plug-ins built for 0.2 keep loading only if their `core` range allows 0.3; the built-in plug-ins and the examples now declare `>=0.1,<0.4`.

New:

- Custom checks can read Elasticsearch with ES|QL (source `elasticsearch`): a query that returns a numeric `value` column and optional `by` columns, run for a window of 5 to 1440 minutes passed as `?start` and `?end`, with the same comparisons, 1 day and 1 week baselines, Preview, presets and editor as PromQL checks, and a Kibana link when the instance has an https UI address.
- A CSP reports preset: a newly blocked host (first seen within a day, at least 10 reports in the window) and a report jump (at least 100 reports and 5 times the larger of a day and a week ago). It reads compact, spaced and escaped report JSON and leaves out browser extensions.
- Built-in language packs for `tr`, `de`, `es` and `fr` (`de`, `es` and `fr` are a preview), always on; a plug-in's pack for the same language wins text by text. `tools/locale_source.py` prints the English source texts for translators. A text a pack does not translate yet is shown or sent in English.
- A batch jobs example plug-in, `examples/batch-jobs-plugin`: failing, missed and slow scheduled jobs read from one Prometheus timer per job, against the same hour 1 and 7 days earlier, with a page and investigator knowledge.

Changed:

- Slack's Critical cards at once switch is now Post at once from (`slack.post-sev`, `SLACK_POST_SEV`): Critical, Warning (the default), Info or Off. Each post ends with a line that is true for its severity and promises an investigation only while the investigator runs live. Admin marks a value that still comes from the old switch, and saving or Reset clears it.
- The investigator prompt has a Common traps section (a slow outside dependency, a p95 moved by the request mix, provider HTML error pages behind JSON parse errors, 429s that look like 500s), also used for Slack questions. How to work covers short baselines, jumps from outside sends and the team's own probes, and a card that clears is no longer called fixed without a cause. The deploy assessment reads new ERROR lines and user-side numbers in a stated order and skips stale cached copies. Plug-in knowledge gained working notes for ES|QL, Prometheus, Kubernetes, Kafka, PMM and Website.
- Two English labels changed so language packs can translate them apart: the open button of a Slack queue row is now View, and the catalog entry button is now Change... (the Open status and the Change column keep their names).

## 0.2.2 (2026-11-19)

Plug-in fields you pick from a list, starting with Cloudflare, names that follow single sign-on and can be edited, Access & sign-in as tabs, and compact custom checks.

**Upgrade notes:**

- At their next SSO sign-in, people whose name is still the setup default ("Admin") or their email address get the name from the identity provider. A name someone chose is kept.
- Accounts that already sign in with SSO keep following the identity provider's name until someone edits it.

New:

- Plug-in fields can offer their values as a list: the field shows chips and a searchable list, free text still works and is marked "not in the list". Cloudflare lists the components of its public status page and the zones the token can see. Plug-ins declare it with `Field.suggest` and `Field.separator`; see docs/extending.md.
- Admins rename people in Admin > Access & sign-in, and everyone can rename themselves from the user menu.

Changed:

- Names come from the identity provider at sign-in (name, else given and family name) unless someone chose one. Control and bidirectional characters are removed from names.
- Admin > Access & sign-in shows People, Single sign-on, API tokens and MCP sign-in as tabs, one at a time, kept in the address.
- Custom checks: a preset set folds into one header line with a summary such as "2 failing, 1 not run yet" and opens by itself when a check fails or has an open card. Rows are shorter and drop the repeated preset prefix.
- Admin > Integrations: a plug-in without checks no longer says "No checks", and a tile with several instances reads "N instances".
- The "How to set it up" box has a chevron that shows it opens.
- The attribution line is centred on the sign-in and setup pages.

## 0.2.1 (2026-11-19)

Setup guides for every plug-in, a SharePoint and OneDrive knowledge plug-in (preview), a Company knowledge group in Integrations, a daily check for new versions, and the "powered by" line on Slack and Teams posts.

**Upgrade notes:**

- Without the Enterprise hide switch, Slack and Teams posts start ending with the "powered by" line.
- The server makes one request a day to `api.github.com` unless `SKOPAX_UPDATE_CHECK=false`.
- Removed: the switch "Powered by on Slack and Teams posts" (`core.chat-attribution`, `SKOPAX_CHAT_ATTRIBUTION`). A stored value is ignored.

New:

- Every plug-in has a "How to set it up" box in its side panel in Admin > Integrations: the steps to create the key, token, service account or app it needs, with the least permissions it checks, and a link to the vendor's guide. docs/plugins.md carries the same steps. Plug-ins declare it with `Plugin.setup`; custom plug-ins can too.
- SharePoint and OneDrive (preview): the investigator searches one SharePoint or OneDrive folder by document name and path and reads Markdown, text and Word files there, masked and cited with links. It signs in as a Microsoft Entra app that needs only `Sites.Selected` and a read grant on one site; every read is checked to lie inside the folder. It replaces the "Coming soon" catalog entry. Not yet tried on a live tenant.
- A Company knowledge group in Admin > Integrations, shown first, with a Knowledge chip, and as the first group of + Add integration: SharePoint and OneDrive, Confluence, GitHub with its Docs field, and outside MCP servers that are knowledge sources. Plug-ins declare it with `Plugin.company_knowledge`.
- A daily check for new versions: once a day one server per install reads the 20 latest releases from GitHub, sending only a User-Agent and an Accept header. Admins see a notice with a Release notes link and a dot on the version line when a newer release exists; it is a warning when a newer release's notes have a line starting with `Security:`. On by default; off in Admin > Features (Check for new versions) or with `SKOPAX_UPDATE_CHECK=false`, which makes no request at all.

Changed:

- Every message Skopax starts in Slack and Teams ends with the "powered by" line whenever the page shows it; a reply to a person never carries it. Only the Enterprise switch Hide "powered by Skopax" removes it, everywhere at once.
- Admin > Integrations shows custom plug-ins and built-in plug-ins in their own headed groups. Chat and Connect MCP no longer carry a New badge.
- The Google Ads developer token is optional, since Google no longer requires it; a token that is set is still checked and sent.
- The attribution line on the card page is centred.
- Dependabot proposes Docker base image updates at once, so a fixed base reaches the image without a 14-day wait.

## 0.2.0 (2026-11-19)

Custom PromQL checks with presets, native GitHub and SonarQube plug-ins, Spring Cloud Config as a settings source, a Teams to Slack echo, a redesigned Access page, and a vulnerability scan of every release image.

**Upgrade notes:**

- Auto-resolve now closes a cleared card nobody acted on after 6 hours instead of 3 days. To keep the old window, set `core.auto-resolve-hours` to `72`; an old `core.auto-resolve-days` value is still read while the hours are not set.
- `routes` is now a reserved core prefix, so a third-party plug-in with the id `routes` no longer loads.
- `custom-checks` is a new core pass: a third-party check with the id `custom`, `custom-checks` or `custom-checks-<anything>` is refused, and so is an instance named `custom-checks` or `custom-checks-<anything>`.
- New built-in plug-in ids `github`, `sonarqube` and `spring-config`, and the new reserved names `sonarqube` and `ghdeploy` (each alone or followed by a hyphen), can clash with a third-party plug-in or an instance of that name.
- Slack lines and DMs that named "Claude" as the actor, such as an assignment made with the investigator's API token, now say "Investigator".
- `RunState` values must be JSON data (dicts with text keys, lists, texts, numbers, true, false, null); anything else is refused with a `ValueError`.
- Plug-in `core` ranges: a plug-in that declares `>=0.1,<0.2` is refused as incompatible on 0.2.0. The built-in plug-ins now declare `>=0.1,<0.3` (GitHub, SonarQube and Spring Cloud Config, new in 0.2, declare `>=0.2,<0.3`). Widen your own plug-in's range once it runs on 0.2, or set `>=0.2,<0.3` when it uses something new in 0.2. The example plug-ins and their Dockerfiles are pinned to 0.2.0.

New:

- Custom PromQL checks: write a check in Admin > Integrations against a Prometheus instance or a Grafana datasource, with a threshold, baselines (a day or a week earlier, as a ratio or a difference), `by` labels, "fire after N passes" and a Preview. Cards, Slack posts and History lines take their texts from the message catalog. At most 50 checks; a source that times out is marked down for the rest of the pass, so it does not hold up the others. A new kit helper, `kit.grafana`, finds a datasource by name and queries through the Grafana proxy. See [Custom checks](docs/custom-checks.md).
- Check presets: "Add from preset" on the custom checks section picks a preset, takes the instance, a cluster name and label matchers, previews each check and adds them all at once. It ships with Couchbase (15 checks through a Prometheus that scrapes the Couchbase exporter) and Managed Redis (9 checks for redis_exporter metrics), each with a check that opens a card when the query finds no series at all. Preset checks are normal checks: editable, grouped on the page and counted toward the 50-check cap.
- GitHub plug-in: GitHub and GitHub Enterprise Server with a fine-grained read-only token. Deployment failure cards, releases on the Releases page, investigator tools (file, search, blame, compare, PRs, checks, docs) and links to commits, PRs and deployments. GitLab and GitHub can both be on: GitLab is asked first, and an outage on one host holds back only its own deploys.
- SonarQube plug-in: a daily quality-gate check that opens a card when a project's gate turns red. Projects already red on the first run are recorded quietly. Test connection checks the token and the Browse permission.
- Spring Cloud Config as a settings source: set `SPRING_CONFIG_URL` (with the app, profile, label, basic auth and a key map) and Skopax reads its settings and instances from a Config Server, refetches them every 15 minutes and resolves placeholders.
- Teams to Slack echo: when a card goes to a Microsoft Teams channel or any other outbound route, Slack gets the card too, and one line in its thread says where it was sent, such as "Sent to seo on Microsoft Teams (tag: seo)". Off by default: `routes.slack-echo` (Admin > General > Slack, or `ROUTES_SLACK_ECHO`), and each route can use the default, on or off. It never bypasses Slack's own rules: a held or pending draft, a muted card or auto-send switched off waits for Slack's normal post, then adds the line.
- Plug-in options: Elasticsearch (log-flow index, message mask, per-service APM p95 overrides, Discover columns), PMM (split users, QAN app pattern, host header, link base), Airbyte (connection match, severities, workspaces), Website (tracking params, kept headers, paths to skip, user agent), Cloudflare (which checks run per zone), Capacity (disks from PMM) and Redis (peer names). Link hosts accept an exact form, `=example.com`.
- Plug-in seams: a digest post without a card (`Sweep.digest`); a plug-in's own clear note and outcome (`Sweep.cleared`), sent through core's clear note so it follows the clear-note switch and settle delay; per-run state (`ctx.run_state`, JSON values of at most 64 KiB each); save-time field checks (`Field.check`), with secrets echoed back masked; a knowledge index with a `plugin_knowledge` tool, and `Knowledge(inline=True)` for short notes every run needs (at most 2000 characters each and 6000 in total, the card owner's notes up to 16000); a frozen card view for `card_text` hooks, which makes the card-text pass faster; and own auto-resolve windows for card kinds (`Plugin.auto_resolve`).
- API token expiry: a token can be made with an expiry, a warning chip shows 7 days before it expires, and an expired token is refused.

Changed:

- Auto-resolve: cleared cards auto-resolve after 6 hours by default (`core.auto-resolve-hours`, Admin > Cards). VWO A/B test verdicts and Google Ads disapproved ads auto-resolve 1 day after they first appeared, set per plug-in. `core.auto-resolve-days` is deprecated.
- Event causes: the investigator can record that a card was caused by an event (a push, a deploy, a job) or that it recurs. A card caused by an event that goes quiet says "Stopped after the event" instead of "Back to normal" in the clear note, the Slack post status, the reminder, the board and the card page, and the card shows its cause. An event card with no ticket waits 6 hours more before auto-resolve (12 hours with the default window).
- Slack auto-send: the guard that holds drafts with copied log text skips the notes Skopax wrote itself, such as "Marked as known noise", so those drafts are no longer held for no reason; mutes and history rows from 0.1.0 written by Skopax are marked in the background after start. Every hold is logged at INFO, and so is "auto-send is off", once per off period.
- Page: an Untagged filter; sidebar groups (Observe, Activity, Connect, More) fold, and the fold is remembered; Settings and Admin moved into the account menu, with the Admin count shown there; a clearer Resolve form (Fixed, No action needed, Duplicate, with a hint for each); an open card no longer flickers on the 15-second refresh and keeps its scroll, focus, open forms and typed text; every Slack draft says in plain words why it was not sent yet, under the draft, in the tag tooltip and in the Slack queue.
- Access page: sections for People, Single sign-on, API tokens and MCP sign-in, with a sticky section strip that shows counts and a warning dot; a people table with role chips, sign-in method badges, search, a role filter and a keyboard row menu, with the last admin's controls locked and the reason shown; allowed email domains as chips; one Save per card; loading and error states per section.
- SSO sign-in button: "Log in" and the SSO button are full width, with "or" between them. The button text is set in Admin (`sso.button-text`, at most 40 characters), and the icon (`sso.button-icon`) is `auto` (from the issuer: Microsoft gets its official mark with the wording Microsoft requires, other providers a letter tile, unknown ones a key), `company` (an uploaded PNG, WebP or plain SVG of up to 64 KiB; SVGs are checked against an allowlist and served sandboxed) or `none`.
- Chainlit 2.12.0: drops `audioop-lts` and clears the scanner findings GHSA-w3fx-mc44-mf6j and GHSA-hvfh-5mj3-5f3j. 0.1.0 could not be reached through them: Chainlit's MCP feature is off and the chat page is behind Skopax sign-in.
- Anthropic SDK 2.5.0.
- README: the Quickstart leads with the published image, with a no-clone Compose file; a plug-ins section lists the built-ins; pictures are smaller and follow the reader's theme.

Fixed:

- The Slack fallback text of a card is escaped, so a service or label text cannot ping a channel.
- The logo goes to the board.
- Page scan: carousels (Swiper, Slick, Splide, Bootstrap, Flickity, Owl, scroll-snap) no longer report cut text, and an identity transform no longer hides a real cut.
- `/favicon.ico` is served (16, 32 and 48 px), for tools that do not read the SVG icon.
- Regex options run with a time limit, and a mask that backtracks badly is refused.

Security:

- Image vulnerability scan: every release image is scanned with Grype before it is promoted. A fixable Critical or High finding stops the release, and the results go to code scanning. A weekly scan of `latest` keeps one issue open, "Image scan: fixable findings in latest", while fixable High or Critical findings exist. Accepted findings go in `.grype.yaml` with a reason and a review-by date, and the weekly run fails once a review-by date has passed. The scan covers the amd64 image.
- Link-host checks read ideographic full stops as '.' before matching.
- A field check never shows a secret, even partly or across lines.
- Website kept headers refuse cookie, auth and secret-like header names.

## 0.1.0 (2026-11-18)

The first stable release. Skopax watches your services and tools, turns problems into cards your team can act on, and helps investigate them with AI. It runs self-hosted in one Docker image.

- Cards from checks on Prometheus, Elasticsearch, Kubernetes, databases, sites and more, with a page to triage, group, assign and resolve them.
- An AI investigator (Anthropic) that reads logs, metrics and code to explain a card, and a chat that answers questions about your systems.
- Slack: cards and deploy reports in a channel, answers in threads, buttons for the common actions. An example app manifest is in `examples/slack-app-manifest.yaml`.
- Admin: integrations as searchable tiles with one form for every integration, feature switches, built-in accounts and SSO (OIDC) with sign-in limits, API tokens, MCP access.
- Plug-ins: a stable contract for checks, tools, settings sources, migrations, locales, card types and deploy reports, with three example plug-ins in `examples/`.
- The image for `linux/amd64` and `linux/arm64` on GHCR and Docker Hub, with the licence, notices and a software list inside.

Since 0.1.0-rc.3: no code changes; the version, the README status, the image wording, and the image workflow skips the Docker Hub copy with a notice when its secrets are not set.

## 0.1.0-rc.3 (2026-11-18)

**Upgrade note:** logs are now readable lines by default, in colour. To keep the JSON lines of earlier releases for a log store, set `SKOPAX_LOG_FORMAT=log4j` (the same fields as before) or `json` (a new flat shape), or pick Log format in Admin > General.

- Admin > Integrations: a redesigned add and edit form for every integration, with sections, switches, segmented choices with readable labels, Show for secrets, certificates folded under Advanced and a footer that stays in view. Plug-ins can name sections and label choices (`Field.group`, `Field.choice_labels`).
- Logs: three formats (`text`, the default, `json` and `log4j`), colour, and level, set by `SKOPAX_LOG_FORMAT`, `SKOPAX_LOG_COLOR`, `SKOPAX_LOG_LEVEL` and `NO_COLOR` at start, and live from Admin > General.
- Investigator: room for 80000 characters of plug-in knowledge (was 24000), with the knowledge of the plug-in that raised the card first, and one line naming any plug-in left out.
- Role resolvers: a failing plug-in role resolver is asked once more, and a person whose role it gave in the last 60 minutes signs in with that role while it is down (Admin > General, 0 refuses as before). Each failure is logged once.
- Website page scan: a box counts as cut off at the screen edge only when text, links or controls cannot be reached, not when a rounded box crops decoration, a screen-reader label or a text ending in an ellipsis; several cut boxes on one page make one sentence.
- Card sentences from plug-ins are asked once per pass instead of once per card, so a busy pass no longer runs out of time.
- Admin > Access: a new Add user form (role as segments, inline messages, Show for the password). With single sign-on on, a person can be added with no password, as an SSO account that opens on their first sign-in with the role picked. Two new sign-in settings: New accounts (anyone the provider signs in, or only people added first) and Allowed email domains.
- Sign-in: while Allowed email domains or New accounts limits who gets in, only the provider's own verified `email` claim counts, on the page and over MCP. The first sign-in to an account added in Admin needs a verified email equal to it, and Add user refuses an SSO account outside Allowed email domains.
- Logs: at DEBUG the database driver and the HTTP and model libraries stay at WARNING, and a non-empty `NO_COLOR` always wins over a colour from Admin or a settings source.
- Slack: an auto-sent post names who drafted it: the product for the server, the token's label for an API token, and Investigator otherwise.
- Admin > Integrations: a search box (`/` jumps to it), compact tiles with a status dot and a short health line, an Add integration list with descriptions and what is already added, and instances shown as cards in the form with an empty state.
- Admin > Integrations marks plug-ins that are not built in as Custom (on the tile, in the form and in the add list), with their author, version and package, and a Custom filter.
- Two fuller example plug-ins next to Hello: `examples/couchbase-plugin` (through a Prometheus instance, with day and week baselines, rules, card types, links, a page and an investigator tool) and `examples/managed-redis-plugin` (through a Grafana datasource).
- Pictures of the product in the README and the docs: the Board, a card with its investigation, the chat, Slack, and the integration form, in light and dark.
- Pull requests: CONTRIBUTING welcomes them under the DCO, and two checks run on each one, a Conventional Commits title and a sign-off on every commit.
- Sidebar: plug-in rows lose the "plug-in" pill (the rail tip and the search still say plug-in), opening More scrolls its rows into view, and the sidebar fades at its foot while more rows are below.
- Features: a warning under a feature card that wraps over several lines reads as one tidy box.

## 0.1.0-rc.2 (2026-11-18)

Plug-in seams for layer images (own card types, an instance writer for migrations, card sentences, daily checks, cards kept off routes, the in-cluster instance name, chat UI texts per language, deploy-report hooks, required migrations), esql and group tool fixes, deterministic tests, an example Slack app manifest.

## 0.1.0-rc.1 (2026-11-18)

The first public release candidate.
