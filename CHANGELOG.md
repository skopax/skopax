# Changelog

Skopax follows semantic versioning. Releases are listed newest first.

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
