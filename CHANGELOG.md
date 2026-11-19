# Changelog

Skopax follows semantic versioning. Releases are listed newest first.

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
