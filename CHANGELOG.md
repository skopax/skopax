# Changelog

Skopax follows semantic versioning. Releases are listed newest first.

## 0.4.6 (2026-11-21)

A clearer mark for cards that went quiet, old Slack posts redrawn, the headless browser as its own plug-in, a search box in the top bar with ticket completion, a notification bell, Admin > Workspaces and list editors for comma settings.

**Upgrade notes:**

- Slack and Teams: a card that stopped firing but is still open now shows :large_yellow_circle: "alert went quiet" (Turkish "Alarm sustu") instead of :eyes: "stopped firing - still open". The check mark still appears only when the card is closed.
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
