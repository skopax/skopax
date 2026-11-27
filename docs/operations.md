# Operations

Running Skopax: the image, agents, the probe, the version check, logs, proxies and recovery.

## The image

The image is `ghcr.io/skopax/skopax`, built for `linux/amd64` and `linux/arm64`. A release candidate is tagged `X.Y.Z-rc.N` only, such as `0.1.0-rc.1`. A release is tagged `X.Y.Z`, `X.Y` and `latest` on GHCR and Docker Hub, where the mirror is `docker.io/skopax/skopax`. If the release environment has no Docker Hub secrets, the image workflow skips the Docker Hub copy with a notice. Pin an exact tag in production.

- **Base.** The image is distroless: it has Python and Skopax, but no shell and no `pip`. It runs as user 1001 and listens on port 8080. Add plug-ins as a layer image (see [The example plug-in: Hello](extending.md#the-example-plug-in-hello)).
- **Database.** Skopax needs a MongoDB, named by `MONGO_URI` (default `mongodb://localhost:27017`). The compose file uses `mongo:8.0.4`.
- **Browser (optional).** Website plug-in screenshots and page scan need a headless Chrome service. The compose file adds it as `browser` with `chromedp/headless-shell:157.0.8103.15`, `restart: unless-stopped` and `shm_size: 1gb`, and publishes no port. Set Browser URL on the Website instance to `http://browser:9222` (see [The browser service](plugins.md#the-browser-service)).
- **Health.** `GET /actuator/health/liveness` answers 200 while the process runs, and the image's own health check calls it. `GET /actuator/health` also pings MongoDB: it answers 200 with `UP`, or 503 with `DOWN` while the database cannot be reached, and names the commit the image was built from. `GET /actuator/health/readiness` gives the same answer, for a readiness probe.
- **First admin.** Every API route answers 503 until a first admin exists, even with the bootstrap token `SKOPAX_TOKEN`. Create the admin on the setup page, or set `SKOPAX_ADMIN_EMAIL` and `SKOPAX_ADMIN_PASSWORD` when nobody opens a browser (see [First run](../README.md#first-run)).
- **Labels.** The image carries the OCI labels `org.opencontainers.image.title` (`Skopax`), `licenses` (`AGPL-3.0-only`), `source` (`https://github.com/skopax/skopax`), `url` (`https://skopax.io`), `version` and `revision` (the commit it was built from).
- **Licences.** `/usr/share/doc/skopax/` holds `LICENSE`, `NOTICE`, `THIRD_PARTY_NOTICES.md`, `third-party-python.md` (every Python distribution in the image with its licence and licence texts) and `third-party-debian.md` (every Debian library copied in, with its package, its reviewed licence and its copyright file), plus `third-party-bundled.md` (every native library a Python wheel bundles, with its reviewed licence) when a wheel bundles one. Each Debian package's copyright file is at `/usr/share/doc/<package>/copyright`. The Python interpreter is CPython 3.15, under the PSF-2.0 licence, with its licence text at `/usr/local/lib/python3.15/LICENSE.txt`, and the distroless base keeps the copyright files of its own packages under `/usr/share/doc/`. See [The licence gate](development.md#the-licence-gate).
- **Scanning.** Every release image is scanned with Grype before it is promoted, and the release stops on a High or Critical finding that has a fix. A weekly scan of `latest` opens one issue, "Image scan: fixable findings in latest", while it finds any, and closes it when the scan is clean. The SARIF report is uploaded to code scanning. The scan covers the amd64 image only. Findings that no release can fix yet are not reported; accepted ones are listed in `.grype.yaml` with a reason that ends in a review-by date.
- **Left out.** The standard library modules `readline`, `dbm.gnu` and `dbm.ndbm` are not in the image, because their libraries are GPL-3+ or Sleepycat. `dbm.sqlite3` and `dbm.dumb` remain.

## Upgrades

1. **Back up the database.** With Compose, `docker compose exec -T mongo mongodump --db skopax --archive > skopax-backup.archive` writes one archive file; with a MongoDB of your own, run `mongodump` against it the same way.
2. **Pin the new exact tag** and keep the same `SKOPAX_SECRET_KEY`. With Compose, check out the new release tag in your clone and run `docker compose up -d --build`. With `docker run`, remove the container (`docker rm -f skopax`) and start it again with the same options and the new tag.
3. **Migrations run at start**, one pod at a time: the pod that starts first takes a 5-minute lease, renewed while it works, and runs the migrations of Skopax and of each plug-in that has some; the other pods check every 5 seconds and wait up to 5 minutes for it. A failed migration of Skopax stops the start, and so does a plug-in migration marked required that is not applied for any reason: it failed, an earlier migration of the plug-in failed, the plug-in is broken at load, locked or switched off, or another pod held the lease past the 5 minutes or the database connection dropped. When the database cannot be reached at start, a plug-in that declares a required migration stops the start too; with none, the pod serves and runs the migrations once the database is back. Kubernetes then restarts the pod. Any other failed plug-in migration breaks only its plug-in (see [Migrations](extending.md#migrations)).
4. **Keep the backup** until the new version runs well. There is no downgrade path: an older version may not read a database a newer one has migrated, so going back means restoring the backup (`mongorestore --archive`).
5. **Plug-ins.** If you run a layer image with plug-ins, rebuild your layer images on the new tag (see [The example plug-in: Hello](extending.md#the-example-plug-in-hello)), and keep each plug-in's `core` range in step with it.

## Agent mode

An agent reads a cluster for a main Skopax that cannot reach it directly. Start the same image with:

- `SKOPAX_MODE=agent`;
- `SKOPAX_AGENT_TOKEN`: the bearer token the main Skopax must send;
- `SKOPAX_AGENT_RELAY_HOSTS`: a comma-separated list of hosts this agent may relay calls to. Empty by default, so an agent relays to nobody until it is set.

An agent uses no database. `GET /actuator/health/liveness` and `GET /actuator/health/readiness` both answer 200 with `UP` while the process runs, and `GET /actuator/health` adds `mode`, `kube` and `token`. All three are open without the token.

On the main side, add the agent in Connect > Integrations: "+ Add integration" > Kubernetes, or "+ Add instance" on the Kubernetes tile once it is connected. Choose mode `agent` and enter the agent's URL and the token. The optional Relay field names another agent instance that passes the calls on. The fields another plug-in reads on the agent, such as `elastic.logging-url` and its API key, are set on the same instance and pushed to the agent; they are the old way. The better way is an instance of that plug-in, such as an Elasticsearch instance, with `Through agent node` set to this agent: its own settings are pushed for its reads and kept apart from other instances (see [Through an agent node](extending.md#peers-clients-and-state)). For one read, any key pushed for it replaces that read's own config on the agent whole: the agent then uses only the pushed keys of that read, and its own config only for a read that got no pushed key. The `local` instance (mode `in-cluster`) appears by itself when Skopax runs inside a cluster, as the primary. `SKOPAX_KUBE_LOCAL_NAME` gives it another name, for example the name a settings source or older data already uses for this cluster, so that the cluster is not read twice under two names. The name follows the rules of [Instances](plugins.md#instances): a reserved or malformed name stops the start, and the variable is ignored outside a cluster. Renaming makes the new name the primary, as moving the primary in Admin does. When Admin had changed the old instance, for example switched one of its checks off, an instance under the old name stays behind that cannot run, since it has no URL; delete it in Connect > Integrations.

## Probe

The probe checks, from this server, that what the integrations depend on can be reached. Every 15 minutes and at start, it asks each instance of every serving plug-in for its probe targets (a name and an `http` or `https` URL each) and calls every target: name lookup, connection, TLS, then one HTTP request. Any HTTP answer below 500 counts as reachable. TLS is checked against the system's CA certificates, or against the instance's own CA certificate when it has one. Each target has 8 seconds, so one that hangs never holds up the others. A failure is a short fixed code, such as `dns: gaierror`, `timeout` or `http 503`, never the error's own text.

Besides the integrations' targets, the probe adds some of its own, shown beside them in the same report and on the Tools page. They are only reachability checks: the same name lookup, connection, TLS and one plain HTTP request, with no API key, token or other credential, so any answer below 500 (an unauthenticated 401 or 404 included) counts as reachable.

- **Slack API**, `https://slack.com/api/api.test`, while the Slack bot token and channel are set.
- **Anthropic API** and **OpenAI API**, each while some use runs on it and its key is set. OpenAI-compatible connections are not probed. When the environment gives the provider SDK a custom base URL (`ANTHROPIC_BASE_URL` or `OPENAI_BASE_URL`, http or https, no user name or password), that address is probed instead of the default one, with any query string or fragment removed first; a custom address that is not valid shows as a `url` failure instead of falling back to the default host.
- **Google APIs**: every `*.googleapis.com` and `*.google.com` host that a serving Google plug-in declares (for example Analytics, Tag Manager, Crashlytics, Safe Browsing), once each. A plug-in that is switched off, or has no enabled instance, adds none.
- **Extra probe targets** (`core.probe-extra`, or `SKOPAX_PROBE_EXTRA`): your own addresses, one `name=url` per line, for example `status=https://status.example.com`. At most 20. A name is 1 to 40 letters, digits, spaces, dots, dashes or underscores and is unique (case ignored); an address is `http://` or `https://` and carries no user name or password. Admin > Settings lists it under Monitor rules as Extra probe targets, and a list that breaks a rule is refused on save with the entry number. A bad value from the environment is used up to the entry before the bad one. A name may not be Slack API, Anthropic API or OpenAI API, or end in `.googleapis.com` or `.google.com`, because those names belong to the built-in targets. Only admins can set the list, so private and loopback addresses are allowed, and a redirect from a target is followed.

These targets report as plug-in `probe` (instance `slack`, `anthropic`, `openai`, `google` or `extra`). No tool depends on them, so a failing one shows in the report but never marks a tool unreachable.

The report is part of `GET /api/server`, under `reach`. Admins see each target's URL and error; everyone else sees names, results, times and the days left on a certificate. The Tools page marks a tool unreachable when every probe target it depends on was down at the last probe.

The probe is the core setting `core.probe` (Admin > Features, Other switches, or `SKOPAX_PROBE`), on by default and live. While it is off, nothing is probed, the last report is not shown and no tool reads unreachable.

**Certificate cards.** The core pass `certs` turns the report into one card per HTTPS host: a warning below `warn_days` (21) days to expiry and a critical card below `crit_days` (7), set in the Certificates rules (`core.certs`, or `SKOPAX_CERT_RULES`). Cards are made for HTTPS targets whose certificate verified. An expired certificate keeps a critical card until it is renewed, and any other TLS failure shows in the report instead. Hosts in Certificates not watched (`core.cert-skip`, or `SKOPAX_CERT_SKIP`) get no card but still count as checked, so a card they already have closes.

**Run now.** `POST /api/probe` with an API token of the operator role or above runs the probe at once and answers its report, with URLs and errors for an admin token only; while the probe is off it answers 409. An operator can run one plug-in check at once with `POST /api/checks/<plugin>/<check>/run`. It answers 202 and writes an activity line, 404 for an unknown plug-in or check, and 409 when the plug-in does not serve, when the check is switched off for all of its instances, or, for a once check, when it is switched off on the primary instance or the primary instance is not serving.

## Logs

Skopax writes one line per log record to standard error.

- **Format.** `SKOPAX_LOG_FORMAT` picks one of three formats. Use `json` when shipping to a log store.
  - `text`, the default: one readable line per record with the time in UTC with milliseconds, the level, the logger and the message. An exception follows on the next lines as its trace.

        2026-11-18T14:05:12.345Z WARN  skopax.roles  the role resolver of plug-in crm failed: RoleLookupDown

  - `json`: one flat JSON object per line with `time`, `level` (`DEBUG`, `INFO`, `WARN`, `ERROR` or `FATAL`), `logger`, `message` and `thread`, plus `error` (`type`, `message`, `trace` and `cause`) when there is one.

        {"time": "2026-11-18T14:05:12.345Z", "level": "WARN", "logger": "skopax.roles", "message": "the role resolver of plug-in crm failed: RoleLookupDown", "thread": "MainThread"}

  - `log4j`: the Log4j JSON layout, for pipelines built for it.

        {"timeMillis": 1795010712345, "thread": "MainThread", "level": "WARN", "loggerName": "skopax.roles", "message": "the role resolver of plug-in crm failed: RoleLookupDown", "endOfBatch": false, "loggerFqcn": "logging.Logger", "threadId": 8460, "threadPriority": 5}

- **Colour.** `SKOPAX_LOG_COLOR` is `always` (the default), `auto` (only when the log goes to a terminal) or `never`, and a non-empty `NO_COLOR` turns colour off, also over a colour from Admin, the config file or a settings source. Only text lines have colour: the level (DEBUG dim, INFO green, WARN yellow, ERROR and FATAL bold red) and the logger (dim). The message stays plain, with its control characters shown escaped (such as `\n` and `\x1b`), so one record stays one line. The JSON formats never carry colour.
- **Level.** `SKOPAX_LOG_LEVEL` is `DEBUG`, `INFO` (the default), `WARNING` (or `WARN`) or `ERROR`. The database driver and the HTTP and model libraries (`pymongo`, `httpcore`, `httpx`, `urllib3`, `anthropic`, `openai` and `mcp`) stay at WARNING at every level, so DEBUG never logs database commands and replies, such as password hashes and session documents.
- **Bad values.** A value that is not one of these starts with the default and one warning that names it.
- **At start and live.** The variables set the log from the first line of a start. Once the settings are read, Admin > Settings has Log format, Log colour and Log level (`core.log-format`, `core.log-color` and `core.log-level`, which the config file or a settings source can also give). A value saved there wins over the variable, except that a non-empty `NO_COLOR` keeps colour off (Log colour then reads `off (NO_COLOR)` in Admin), and applies from the next line on, without a restart; Reset goes back to the variable. The first lines of a start, written before the settings are read, always follow the variables or the defaults. An agent has no Admin settings and follows the variables only.

## Search by meaning

Search by meaning finds earlier cards, runs, deploys, notes, pages and error messages by what they say, in any of the model's languages, not by the words they use. A question in Turkish finds a German or English postmortem. It adds the header search box, the investigator's `find_similar` tool and a short list of similar earlier incidents in the brief of a card investigation. It is off by default, it costs no AI spend, and without an embedding service the word search is all there is. The settings are in [Search by meaning](configuration.md#search-by-meaning) and what is stored is in [What is embedded](privacy.md#what-is-embedded).

It needs one extra service: an embedding server that speaks `POST /v1/embeddings`. Skopax is built for Hugging Face text-embeddings-inference (TEI) 1.10.0 serving `intfloat/multilingual-e5-small` (384 dimensions, about 100 languages including Turkish, English, German, Spanish and French). The service runs inside your network; it is not an AI provider, and nothing is sent to a company outside it unless you set `SKOPAX_EMBED_ALLOW_PUBLIC`.

### Deploy with Compose

The compose file has an `embed` service under the profile `semantic`, so a plain `docker compose up` does not start it. It runs the TEI image pinned by digest, publishes no port, runs as user 65532 with a read-only root file system and no capabilities, and is limited to 1 CPU and 1.5 GiB.

1. Put the model on disk. The service reads it from a folder, by default `./embed-model` next to the compose file (set `SKOPAX_EMBED_MODEL_DIR` for another folder), mounted read-only at `/model`. Download the eight files of revision `614241f622f53c4eeff9890bdc4f31cfecc418b3` from `https://huggingface.co/intfloat/multilingual-e5-small/resolve/614241f622f53c4eeff9890bdc4f31cfecc418b3/<file>` and check each against its sha256:

   | File | sha256 |
   |---|---|
   | `config.json` | `69137736cab8b8903a07fe8afaafdda25aac55415a12a55d1bffa9f581abf959` |
   | `tokenizer.json` | `0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39` |
   | `tokenizer_config.json` | `a1d6bc8734a6f635dc158508bef000f8e2e5a759c7d92f984b2c86e5ff53425b` |
   | `special_tokens_map.json` | `d05497f1da52c5e09554c0cd874037a083e1dc1b9cfd48034d1c717f1afc07a7` |
   | `modules.json` | `c6e29747481e8b5dd2b58401966aeac910de39092f90cda9a704b1545f902b04` |
   | `sentence_bert_config.json` | `948201d8329907aae938fa62f9ceeed53f5694dacc2b87b9f3b78b37ee986529` |
   | `1_Pooling/config.json` | `987f7a67a38fa564c849bb5d277c52ab9088a84368fc0be31a354125aebb12a0` |
   | `onnx/model.onnx` | `ca456c06b3a9505ddfd9131408916dd79290368331e7d76bb621f1cba6bc8665` |

   Keep the folder layout (`1_Pooling/` and `onnx/` are subfolders). The whole model is about 465 MB.
2. Make a key and the address Skopax calls in `.env`: `echo SKOPAX_EMBED_API_KEY="$(openssl rand -hex 32)" >> .env` and `echo SKOPAX_EMBED_URL=http://embed:8080 >> .env`. The compose file passes both to Skopax and gives the key to the service as `API_KEY`. Set the key before you start the service: with no key the service answers anyone on the compose network.
3. Start it with `docker compose --profile semantic up -d`, then turn on Search by meaning in Admin > Features. The switch stays locked while the URL is empty.

The compose file pins the amd64 image, `ghcr.io/huggingface/text-embeddings-inference:cpu-1.10.0@sha256:f0a4bb206b9d4877746b5b9f638e138fe683eeb9787c14d541a12c993b562d86`. TEI has no multi-architecture tag, so on an arm64 host change the image to `ghcr.io/huggingface/text-embeddings-inference:cpu-arm64-1.10.0@sha256:446c683eef5bbbdf1eb9899966b8e198921f671e92ee3fe3d7a5f6d770ea0a26`. Do not use a 1.9 release: it writes the API key to its log.

### Deploy on Kubernetes

[examples/k8s/embed](../examples/k8s/embed/README.md) has the manifests: a Deployment, a Service on port 80, a 1 GiB volume, a Job that fetches the model files and fails on any sha256 mismatch, a NetworkPolicy and a Secret for the key. Apply the volume, run the Job, then apply the Deployment; the README has the order. Set `SKOPAX_EMBED_URL=http://skopax-embed` and the same key in Skopax's environment.

The service's own key covers only the embedding routes. `/health`, `/metrics`, `/docs` and the API description answer without one, so keep the service off every network except Skopax's: the example NetworkPolicy allows ingress from the Skopax pods only and no egress. The model name `SKOPAX_EMBED_MODEL` has to match the name the service serves (`--served-model-name`); Skopax refuses an answer that names another model or has vectors of another length.

### Sizing

- **Embedding service:** 1 vCPU and 1.25 to 1.5 GiB of memory. It cannot start with 1 GiB. It holds the model in memory (about 0.9 GiB idle, about 1 GiB at its peak) and the figures here are for one core.
- **Skopax pod:** about 80 MB of memory per 100,000 vectors, up to the cap `SKOPAX_EMBED_MAX_VECTORS` (300,000 vectors, about 230 MB), plus about 50 MB for the numpy library. The index is loaded by the first search or the first run that needs it, and only pods that search keep it.
- **MongoDB:** about 1 KB per vector, so 100,000 vectors are about 100 MB. A vector's size does not depend on its text. A year of an average install comes to roughly 60,000 to 150,000 vectors. Older items beyond the cap are left out of search, the oldest first, and the indexer's status in the state collection says so.
- **Disk:** about 1 GB for the model volume.

### First fill

When the feature is turned on, Skopax adds new items as they arrive and, in the background, the older ones: newest first, at most `SKOPAX_EMBED_BACKFILL_PER_PASS` (default 200) every minute, and `SKOPAX_EMBED_DOCS_PER_PASS` pages. On one cloud vCPU the service manages about 4 to 8 passages a second at 200 tokens and 2 to 3.5 at 400 tokens, so the default of 200 a minute leaves it room for live items. At that pace 100,000 vectors take about 8 hours. Raising the per-pass limit above what the service can do only builds a queue. Search works from the first vector: newer items are found first, and the indexer writes how far the fill is (counts and progress) to the state collection for now. A question is answered in about 15 to 30 ms by the service plus about 10 ms per 100,000 vectors in Skopax.

### Changing the model

Changing `SKOPAX_EMBED_MODEL` or the passage prefix gives the index a new fingerprint. Search switches at once to the part of the new index that exists and says "index rebuilding" with how far it is; the old vectors are removed when the new ones are complete. The index rebuilds by itself when the model or the prefix changes; there is no button for it. The first answer from the service is checked: a different model name or vector length marks the service down with "model mismatch" and nothing is stored.

### When the service is down

After three failed calls in a row Skopax marks the service down for 30 seconds, then for twice as long each time up to 10 minutes, and sends nothing meanwhile. During that time search falls back to word matches with a note, `find_similar` is not offered (a run that already has it gets a message that points to `past_cards` and `past_runs`), and the brief block is skipped. Indexing keeps a queue of changes for 7 days and catches up when the service answers again. Skopax writes and board passes are never held up by the service.

### Air-gapped installs

The CPU image of TEI ignores `HF_HUB_OFFLINE` and would still try to reach the Hugging Face hub for a model name, so do not give it a hub name. Both examples start it with `--model-id /model`, a folder mounted read-only, and name the model with `--served-model-name`. Copy the eight files in from a machine that can download them (and check the sha256 values above), or from your own artifact store, into the folder or the volume. The image itself has to come through your registry mirror, by its digest. The service then makes no outside connection at all.

### Throughput

Requests ask for at most 32 texts, and the service accepts 64 at the same time. The CPU backend runs 8 texts per forward pass, which is why `SKOPAX_EMBED_BATCH` defaults to 8; a larger batch gains nothing. Several Skopax pods share one service: only one pod per minute claims the indexing job, so they do not multiply the load. If search questions time out while a fill runs, lower `SKOPAX_EMBED_BACKFILL_PER_PASS` or raise `SKOPAX_EMBED_TIMEOUT_SECONDS` (the default is 3 seconds).

## Behind a proxy or ingress

Set `FORWARDED_ALLOW_IPS` to the proxy's address or range, for example `10.0.0.0/8`. Without it every client appears to come from the proxy's IP, so 20 wrong passwords from anyone pause password login for everyone for 15 minutes. The log warns when this looks misconfigured. Logins are also limited to 5 failures per email in 15 minutes.

The proxy must pass the original `Host` header through. Skopax compares it with the browser's `Origin` and refuses changes that look cross-site.

## Outbound gateway

If outbound calls to Anthropic or OpenAI go through a gateway of your own, it must pass the `Authorization` header: both clients send the API key as `Authorization: Bearer <key>`, not as `X-Api-Key`. Anthropic calls go to `api.anthropic.com`, OpenAI calls to `api.openai.com`.

## Version check

Skopax asks GitHub once a day whether a newer release exists, and tells admins when one does. It is on by default.

- **What is requested.** One request: `GET https://api.github.com/repos/skopax/skopax/releases?per_page=20`. It runs once a day at 03:00 UTC plus a fixed delay of 0 to 600 seconds that is the same for every pod of one install and differs between installs. It also runs at start, after about 45 seconds, but only when no result is stored or the stored one is older than 20 hours, and one pod claims that run so the pods of an install make one request together; the daily run also skips a result younger than 20 hours. Only releases that are not drafts or pre-releases and whose tag looks like `vX.Y.Z` count. The newest is the highest version, not the latest date. A server that runs a pre-release or a development build does not check.
- **What is sent.** Only what an HTTP client must send: the address and two headers, `User-Agent: Skopax/<version>` and `Accept: application/vnd.github+json`. No install id, no instance name, no usage data. The call goes through the same proxy settings as other outbound calls.
- **What it shows.** Admins see a notice in the notices strip, "Skopax X.Y.Z is available; this server runs A.B.C.", with a Release notes link, and a small dot on the version line at the bottom of the sidebar. When any release newer than the version this server runs marks itself as a security release (see [Releases](development.md#releases)), the notice is a warning, "Skopax X.Y.Z is available and fixes a security issue; this server runs A.B.C. Update soon.", and the dot is the warning colour. Other people see nothing. The result is kept in the database, so every pod shows the same result; the level follows the version each pod runs.
- **When it fails.** An offline server, a rate limit or an answer that is not JSON is ignored: the last good result stays, and there is no notice about the failure and no card.
- **Turning it off.** Switch off Check for new versions in Admin > Features, or set `SKOPAX_UPDATE_CHECK=false`. While it is off, no request is made and the notice and the dot are gone. A value saved in Admin overrides the variable and applies at once.

## Lost setup code

Set `SKOPAX_ADMIN_EMAIL` and `SKOPAX_ADMIN_PASSWORD`, then run `docker compose up -d` (a plain `restart` keeps the old environment). The admin is created from the environment.

## Lost admin password

Another admin can set a new one in Admin > Access & sign-in. With no other admin, use the `SKOPAX_TOKEN` value as a Bearer token (if it is not set, set it and run `docker compose up -d` first):

    curl -X PATCH http://localhost:8080/api/admin/users/admin@example.com \
      -H 'Authorization: Bearer <the SKOPAX_TOKEN value>' -H 'Content-Type: application/json' \
      -d '{"password": "a new password of 12 to 256 characters"}'
