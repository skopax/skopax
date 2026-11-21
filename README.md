# Skopax

[![ci](https://github.com/skopax/skopax/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/skopax/skopax/actions/workflows/ci.yml)
[![image](https://github.com/skopax/skopax/actions/workflows/image.yml/badge.svg)](https://github.com/skopax/skopax/actions/workflows/image.yml)
[![release](https://img.shields.io/github/v/release/skopax/skopax)](https://github.com/skopax/skopax/releases/latest)
[![licence](https://img.shields.io/github/license/skopax/skopax)](LICENSE)

**The AI lookout for your production systems.**

Every team has that one person who always knows why things are happening and what to do. Skopax is that person, for the whole team, at any hour.

Its AI investigator keeps an eye on production. When something breaks, it reads your metrics, logs and recent deploys, then tells you why it broke and what to do next, in plain words. You can also chat with it about prod on its own chat page, ask it in Slack, or reach it from your own AI assistant over MCP.

Behind it, alerts and errors are grouped into one card per problem, so the team can see who is on it.

The name comes from the Greek *skopos*, "lookout": the one who stands on a high place, scans the horizon and calls out what is coming.

Status: 0.4.10 is the current release; 0.1.0 was the first stable one. Since 0.1.0, settings and the plug-in contract follow semantic versioning: before 1.0, no breaking change ships without a note in [CHANGELOG.md](CHANGELOG.md).

## What it looks like

<table>
<tr>
<td width="33%" valign="top">

<a href="docs/images/chat-dark.png#gh-dark-mode-only"><img width="280" src="docs/images/chat-dark.png#gh-dark-mode-only" alt="The Skopax Chat page answering Why is checkout slow since 14:00 with the latency rise, the timeout log lines, the cause in checkout 2.14.0 and a next step"></a><a href="docs/images/chat-light.png#gh-light-mode-only"><img width="280" src="docs/images/chat-light.png#gh-light-mode-only" alt="The Skopax Chat page answering Why is checkout slow since 14:00 with the latency rise, the timeout log lines, the cause in checkout 2.14.0 and a next step"></a>

<b>Chat</b>: ask about prod in plain words; the answer names the metrics and logs it read.

</td>
<td width="33%" valign="top">

<a href="docs/images/card-dark.png#gh-dark-mode-only"><img width="280" src="docs/images/card-dark.png#gh-dark-mode-only" alt="Card 42, checkout p95 latency, open in a drawer: suspects, a recommendation to roll back, the code line that changed, the investigation note and two merged cards"></a><a href="docs/images/card-light.png#gh-light-mode-only"><img width="280" src="docs/images/card-light.png#gh-light-mode-only" alt="Card 42, checkout p95 latency, open in a drawer: suspects, a recommendation to roll back, the code line that changed, the investigation note and two merged cards"></a>

<b>Investigation</b>: a card with its suspects, a recommendation, the code lead and the merged cards.

</td>
<td width="33%" valign="top">

<a href="docs/images/integrations-dark.png#gh-dark-mode-only"><img width="280" src="docs/images/integrations-dark.png#gh-dark-mode-only" alt="Connect, Integrations: connected tiles, and the panel of an MCP server connection listing its read tools switched on, a write tool switched off and the repository it searches"></a><a href="docs/images/integrations-light.png#gh-light-mode-only"><img width="280" src="docs/images/integrations-light.png#gh-light-mode-only" alt="Connect, Integrations: connected tiles, and the panel of an MCP server connection listing its read tools switched on, a write tool switched off and the repository it searches"></a>

<b>Integrations</b>: every connection uses the same form, here an outside MCP server and its tools.

</td>
</tr>
</table>

## Plug-ins: connect what you already run

Everything Skopax watches comes through plug-ins, and more than 30 of them ship in the image. Most are ready to switch on in Connect > Integrations; Spring Cloud Config is set up with `SPRING_CONFIG_URL` instead (see [Configuration](docs/configuration.md#spring-cloud-config)):

<a href="docs/images/plugins-dark.png#gh-dark-mode-only"><img width="420" src="docs/images/plugins-dark.png#gh-dark-mode-only" alt="Connect, Integrations: the plug-ins from GitLab, GitHub, Jira and Slack to Kubernetes with two instances, Cloudflare, Google Ads and an MCP server, each with its switch and health"></a><a href="docs/images/plugins-light.png#gh-light-mode-only"><img width="420" src="docs/images/plugins-light.png#gh-light-mode-only" alt="Connect, Integrations: the plug-ins from GitLab, GitHub, Jira and Slack to Kubernetes with two instances, Cloudflare, Google Ads and an MCP server, each with its switch and health"></a>

| Area | Plug-ins |
|---|---|
| Metrics, logs and alerts | Prometheus, Elasticsearch, Grafana |
| Infrastructure | Kubernetes, Argo CD, Kafka, Redis, PMM (Postgres, MongoDB), Airbyte, Capacity |
| Web and edge | Website, Cloudflare, Varnish, Chrome UX Report, CSP reports, Safe Browsing |
| Apps | App stores, Crashlytics |
| Marketing and analytics | Google Ads, Google Analytics 4, Google Tag Manager, Umami, VWO |
| Code, tickets and docs | GitLab, GitHub, SonarQube, Jira, Confluence, SharePoint and OneDrive |
| Chat and AI | Slack, Microsoft Teams, Langfuse |
| Settings | Spring Cloud Config |

- **Any MCP server.** Connect an outside MCP server and pick which of its tools the investigator may use. The catalog has ready entries for Atlassian, Notion, GitHub and Google Drive.
- **Several of each.** Every plug-in can have more than one instance, such as two Prometheus servers or a Kubernetes cluster per region, each with its own checks and thresholds.
- **Shape it without code.** Admin switches whole features on and off, turns single checks on or off per instance, sets their thresholds, and changes branding and language. Settings can also come from a file or a settings source, so an install can be configured as code.
- **Write your own.** A plug-in is a small Python package against a documented contract: checks that make cards, tools for the investigator, settings and an Admin form, all from one definition. Ship it in your own image built `FROM` Skopax. Start from `examples/hello-plugin`, or from the fuller `examples/couchbase-plugin`, `examples/managed-redis-plugin` and `examples/batch-jobs-plugin`. See [Integrations](docs/plugins.md) and [Extending Skopax](docs/extending.md).

## AI costs: see them, tune them, cap them

The investigator calls the AI provider you pick, Anthropic or OpenAI, with your own key, so you pay the provider directly and Skopax shows where the money goes.

<a href="docs/images/costs-dark.png#gh-dark-mode-only"><img width="420" src="docs/images/costs-dark.png#gh-dark-mode-only" alt="The Skopax Costs page for the last 7 days: 2.31 dollars today of a 10 dollar daily cap, 19.71 dollars over the week, averages per Slack ask and per investigator run, a 79 percent cache hit share, spend per day stacked by source and the by-source table with models"></a><a href="docs/images/costs-light.png#gh-light-mode-only"><img width="420" src="docs/images/costs-light.png#gh-light-mode-only" alt="The Skopax Costs page for the last 7 days: 2.31 dollars today of a 10 dollar daily cap, 19.71 dollars over the week, averages per Slack ask and per investigator run, a 79 percent cache hit share, spend per day stacked by source and the by-source table with models"></a>

- **See it.** The Costs page, under Activity, shows the spend per source: investigator runs, page asks, Slack asks, deploy assessments, small talk, page scans and self-reviews. It also shows the spend per hour for today and per day over 7 or 30 days, the average cost of a Slack ask and of an investigator run, the models used and the cache hit share.
- **Tune it.** Admin > Investigator sets the model and thinking effort for cards, Slack, Chat and the topic gate separately, so the cheap checks can stay on a small model. Prompts are cached between calls, and the Costs page shows how much of each prompt came from the cache.
- **Cap it.** A daily dollar limit is shared by the investigator and the self-review: once it is reached they stop until the next day, and 0 stops them. Page and Slack asks have hourly caps, for everyone together and per person. Chat messages have a cap per person per day, and MCP investigations a cap per person per hour. Each card run, assessment and Slack ask has its own tool-call budget, and web searches and page reads per run are limited too. A warning card that clears and comes back keeps its earlier finding for a set number of hours instead of being investigated again.
- **Stop it.** Set the investigator's mode to off or shadow, or press Stop investigator to halt a run at once.

## Quickstart

Skopax ships as a ready image, so there is nothing to clone or build: `ghcr.io/skopax/skopax:0.4.10`, built for `linux/amd64` and `linux/arm64`. Docker Hub has a mirror of each release, `docker.io/skopax/skopax`; release candidates are published to GHCR only. [Operations](docs/operations.md#the-image) has more on the image.

Run it with a MongoDB of your own. The first line makes the secret key once:

    SKOPAX_SECRET_KEY="$(openssl rand -hex 32)"
    docker network create skopax
    docker run -d --name skopax-mongo --network skopax -v skopax-mongo:/data/db mongo:8.0.4
    docker run -d --name skopax --network skopax -p 127.0.0.1:8080:8080 \
      -e MONGO_URI=mongodb://skopax-mongo:27017/skopax \
      -e SKOPAX_BASE_URL=http://localhost:8080 \
      -e SKOPAX_SECRET_KEY="$SKOPAX_SECRET_KEY" \
      ghcr.io/skopax/skopax:0.4.10

Then open http://localhost:8080. Keep that key, for example in your password manager (`echo "$SKOPAX_SECRET_KEY"` prints it), and pass the same value on every start. With another key, Skopax cannot open the integration secrets sealed with the old one, so each has to be entered again, and everyone has to sign in again (see [Settings](docs/configuration.md#settings)).

### With Docker Compose, no clone

Save this as `compose.yml` in an empty folder:

    services:
      mongo:
        image: mongo:8.0.4
        restart: unless-stopped
        volumes:
          - mongo-data:/data/db
        healthcheck:
          test: ["CMD", "mongosh", "--quiet", "--eval", "db.adminCommand('ping').ok"]
          interval: 10s
          retries: 10
      skopax:
        image: ghcr.io/skopax/skopax:0.4.10
        restart: unless-stopped
        ports:
          - "127.0.0.1:8080:8080"
        depends_on:
          mongo:
            condition: service_healthy
        environment:
          MONGO_URI: mongodb://mongo:27017/skopax
          SKOPAX_BASE_URL: http://localhost:8080
          SKOPAX_SECRET_KEY: ${SKOPAX_SECRET_KEY:?run the echo line below first}
      browser:
        image: chromedp/headless-shell:157.0.8103.15
        restart: unless-stopped
        shm_size: 1gb
    volumes:
      mongo-data:

The `browser` service is optional. It is for Website plug-in screenshots and page scan, and it publishes no port. When you add a Website instance, set Browser URL to `http://browser:9222`; drop the service if you do not use the Website plug-in.

Then, in that folder, make the secret key once (it goes to `.env`, which Compose reads on every start) and start it:

    echo SKOPAX_SECRET_KEY="$(openssl rand -hex 32)" >> .env
    docker compose up -d

Read the setup code below with `docker compose logs skopax` instead of `docker logs skopax`.

### From source, with Docker Compose

To build the image yourself, or to try the demo Prometheus, use Docker Compose from a clone of this repository. The first line makes the secret key once and writes it to `.env`, which Compose reads on every start and passes to Skopax, so run that line only once:

    echo SKOPAX_SECRET_KEY="$(openssl rand -hex 32)" >> .env
    docker compose up --build

The compose file builds the image from the clone and starts it with a MongoDB; the key stays in `.env`. Read the setup code below with `docker compose logs skopax` instead of `docker logs skopax`.

Run one Skopax per MongoDB database: that is the supported shape in v0.1. Several pods of one Skopax share their database safely, but two separate copies on one database can, for example, post a retried route message twice.

## First run

A new install has no accounts, so every page sends you to `/setup`.

- Read the setup code from the container logs: `docker logs skopax`. Look for `Skopax setup: open ... and enter the code XXXX-XXXX`. Enter the code, an email, a name and a password (12 to 256 characters) to create the first admin.
- Or skip the form: set `SKOPAX_ADMIN_EMAIL` and `SKOPAX_ADMIN_PASSWORD` in the environment before the first start. That account is created as an admin. Do this when nobody opens a browser, for example in a script or a CI job: until a first admin exists, every API route answers 503, even with the bootstrap token `SKOPAX_TOKEN`.

Admins add more people, change roles, manage API tokens and set up single sign-on in Admin > Access & sign-in (see [Sign in with SSO](docs/sign-in.md#sign-in-with-sso)).

Next, add a first integration: [Add Prometheus](docs/setup.md#add-prometheus) walks through it, and [Run the demo](docs/setup.md#run-the-demo) starts a Prometheus to try it on.

## Documentation

The full guide is in [docs/](docs/README.md):

- [Setup](docs/setup.md): the page, Admin, the first integration and the demo.
- [Configuration](docs/configuration.md): settings, the config file and settings sources, branding and languages.
- [Sign-in](docs/sign-in.md): single sign-on, roles, local accounts and the MCP server.
- [Integrations](docs/plugins.md): the plug-ins that ship with Skopax, their instances and outside MCP servers.
- [Custom checks](docs/custom-checks.md): PromQL and ES|QL checks written in Connect > Integrations, without a plug-in, and the presets for Couchbase, managed Redis and CSP reports.
- [Extending Skopax](docs/extending.md): writing a plug-in, starting from the example in `examples/hello-plugin`, with three fuller examples next to it: `examples/couchbase-plugin`, `examples/managed-redis-plugin` and `examples/batch-jobs-plugin`.
- [Operations](docs/operations.md): the image, logs, agent mode, the probe, proxies and recovery.
- [Development](docs/development.md): the tests, continuous integration, the licence gate and releases.

## Contributing

Issues and pull requests are welcome. Contributions come in under Apache-2.0, the licence they go out under, and every commit carries a `Signed-off-by` line under the Developer Certificate of Origin 1.1 (`git commit -s`). There is no separate agreement to sign. A pull request title follows Conventional Commits, such as `fix: ...` or `feat(slack): ...`.

- [CONTRIBUTING.md](CONTRIBUTING.md): what we accept, the project rules and the sign-off.
- [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md): the Contributor Covenant 2.1. Report a conduct problem to hello@skopax.io.
- [Development](docs/development.md): setting up, running the tests and CI.

## Security

Do not open a public issue for a security problem. Report it privately with GitHub private vulnerability reporting: the **Security** tab of https://github.com/skopax/skopax, then **Report a vulnerability**. [SECURITY.md](SECURITY.md) says what to include, what to expect and which versions get fixes.

## License

Skopax is licensed under the Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE). Bundled icons and their licences are listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

The image carries its licence material in `/usr/share/doc/skopax/`: `LICENSE`, `NOTICE`, `THIRD_PARTY_NOTICES.md`, and the lists the image build generates, `third-party-python.md` (every Python distribution in the image, with its licence and licence texts) and `third-party-debian.md` (every Debian library copied in, with its package, licence and copyright file). The build fails on a dependency whose licence is not allowed; see [The licence gate](docs/development.md#the-licence-gate). The Python interpreter in the image is CPython 3.15, under the PSF-2.0 licence, with its licence text at `/usr/local/lib/python3.15/LICENSE.txt`.

The Skopax name, the Beam X logo and the Skopax wordmark are reserved by [TRADEMARKS.md](TRADEMARKS.md). The logo files themselves are Apache-2.0 like the code. A fork may change the code under Apache-2.0, but when you distribute it or offer it as a service, it must use its own name and drop the logo.
