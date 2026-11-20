# Integrations

The plug-ins that ship with Skopax, their instances, and outside MCP servers.

## Plug-ins

Prometheus, Kubernetes, the observability plug-ins, GitLab, GitHub, Jira, Confluence, SharePoint and OneDrive, Argo CD, Slack, Microsoft Teams, the web, app and marketing plug-ins and the MCP server plug-in ship with Skopax (see [Add Prometheus](setup.md#add-prometheus), [Observability plug-ins](#observability-plug-ins), [Code, tickets and chat](#code-tickets-and-chat), [Web, app and marketing plug-ins](#web-app-and-marketing-plug-ins) and [Outside MCP servers](#outside-mcp-servers)). Others are found in two ways:

- installed packages that register a `skopax.plugins` entry point;
- `SKOPAX_PLUGINS`, a comma-separated list of module paths, each with a `PLUGIN` object.

Connect > Integrations shows a tile for each connected plug-in, with its health and an on/off switch, and "+ Add integration" lists the plug-ins that can be set up. A plug-in is on unless an admin switched it off, and one that needs instances does nothing until it has an enabled, valid instance.

Admins read and change integrations through `/api/admin/plugins`. Everyone else who is signed in reads them through `GET /api/integrations`, which every role may call and which answers 401 without a login. It lists the same plug-ins with only what the read-only page shows: each instance's plain values (choices, switches, numbers), web addresses cut to scheme, host and port (an address that is not http or https shows only as set), other texts with secrets masked (passwords, tokens, keys, `Basic` and `Bearer` credentials, known token shapes, and paths cut from addresses), every secret and every secret-named field only as `{"set": true}` or `false`, which checks are on, and the current thresholds as `rules[].values`. User names, emails and public keys (such as a Langfuse public key or an OAuth client id) are not secrets and stay visible. Every write route stays admin-only.

Below the tiles, Custom checks turns a PromQL query on a Prometheus or Grafana instance, or an ES|QL query on an Elasticsearch instance, into cards without a plug-in (see [Custom checks](custom-checks.md)).

A plug-in that is installed as a package or loaded from `SKOPAX_PLUGINS` shows as Custom in Connect > Integrations, with a Custom chip to list only those; built-in plug-ins carry no mark.

A plug-in that is a company knowledge source (see [Company knowledge](#company-knowledge)) shows a small Knowledge badge on its tile, in its own category like any other tile, and a Knowledge chip right after All lists exactly the tiles that carry it. The badge has states: Knowledge when the investigator can search it, muted Knowledge off while the plug-in is switched off, and a warning Knowledge needs setup when an instance still needs its knowledge field (such as the Docs field of GitHub) or the plug-in has a problem shown on its tile; its tooltip says what to do. Confluence, SharePoint and OneDrive, Jira and GitHub carry it, and so does an MCP server once a connection has its Knowledge source on. A plug-in shows once, so counts and the Knowledge chip count it once. With no custom tile the tiles are one plain grid. "+ Add integration" lists plug-ins in their own categories too, with the same Knowledge badge on every entry that can be a knowledge source.

Editions are `community` and `enterprise`. Skopax runs as `community` unless a plug-in's entitlement hook says otherwise. A plug-in that needs another edition is listed under "+ Add integration" with its edition but cannot be set up or switched on, and it contributes nothing. A plug-in that needs a different Skopax version is listed there too, but cannot be set up; the reason shows when you point at it, and it contributes nothing. A plug-in that fails to load or breaks one of the plug-in rules contributes nothing either; the notices strip in Admin names it and says why. Plug-ins add no web routes.

A plug-in page is a `Panel` with a `label`, a `script` and a sidebar `group`: `main`, `observe` (the default), `activity`, `connect` or `more`. While the plug-in serves, the page is a row at the end of that group, tagged plug-in and drawn with the plug-in's own icon. The label is 1 to 24 printable characters on one line, with spaces at either end dropped. The tab id is lower-case letters, digits and dashes, 2 to 31 characters; it is not a built-in page, not one of the ids the page reserves (`sub-anoms`, `fold-anoms`), and not a page tab another plug-in already declared. A plug-in that breaks one of these rules is not loaded, and the notices strip in Admin shows why.

The page gets an empty `<section id="panel-<tab>">`, and the plug-in's script fills it. The script is loaded with the page, before the section exists, so it listens on `document` for the `skopax:page` event. The event fires once per page, right after its section is added: when the page first learns the plug-in list, or later when the plug-in starts serving. Its `detail` holds `tab` (the tab id) and `section` (the element). If the plug-in stops serving, the row and the section are removed.

Each time the board paints, once at page load and again after every live update, it fires a `skopax:state` event on `document`. Its `detail.state` is the board state, with each section a plug-in writes under its own name (`state.<section>`). The first one fires before any `skopax:page`, so a panel script keeps the latest `detail.state` and renders from it on `skopax:page` and again on every `skopax:state`.

A panel script defines its own helpers, such as escaping and time formatting, and reads nothing else of the page; the one page value it may read is `BRAND.version`.

A panel on a built-in tab only points a tile at that page, so it has no script, fragment or label. Fragments are served but not loaded into the page in this version.

## Instances

A plug-in can have several instances, for example one Prometheus per cluster. One instance per plug-in is the primary. It is the default for tools that take a `cluster` input, and its cards and the names of its checks carry no instance name. The first instance becomes the primary, a source can flag one with `primary: true`, and an admin can move the flag.

Other instances mark their cards with their name as the `cluster`.

Two instances never share a card. The primary's card keys have no instance part (`err|shop/checkout-service`); a card of another instance carries the instance name after `@` (`err|shop/checkout-service@east`), and its ran keys end in `-<name>`. Each instance's pass clears only its own cards. A check reads its own open cards through `ctx.open_cards()` with the keys it raised, without the `@<name>` part; `contract.card_key(raised, ctx.instance)` gives the key such a card is stored under (`contract.KEY_SEGMENT` is the `@`), for a card that names another of its plug-in's cards. Turning an instance off, or deleting it, closes its open cards at once, without a "back to normal" message, and leaves the others alone.

Changing an address of an instance (any field of kind `url`, or the relay of a remote agent) asks for its secrets again: a save that moves the address without re-entering each stored secret that is shown in the form is refused with "Enter the secret again for the new address", and a stored secret of a hidden field (another auth mode) is dropped. An optional link address beside the address the plug-in reads, such as the `ui-url` of Prometheus or Elasticsearch, moves no secret; a required address always asks, even when it is also used for links (Website's `url`, which receives `scan-headers`). A text field that lists hosts a secret goes to counts as an address when its plug-in marks it with `Field(..., address='hosts')`: adding a host, or renaming one, asks again, and removing one does not (Website's `hosts`). The plug-in can name the parser it reads the field with (`hosts_of(value, config)`, which also gets the instance's other values); then an edit that brings a stored host into use, such as splitting two hosts joined by a space, dropping one of more hosts than the plug-in reads, or removing a Website login host that covered its `url` host or one of its `hosts`, asks too. A change of trust asks the same way: a `ca` that is set, changed or cleared, or `ca-pinned` switched off, asks for the secrets that go to that address, with its own sentence ("Enter the secret again for the new certificate setting"), because the pasted certificate decides who receives them. The same certificate sent again with other line breaks is no change, and switching `ca-pinned` on asks for nothing. Clearing an address marked `address='alone'`, whose reads no other address takes over, asks for nothing (Website's `browser-url`); moving it still asks. For an agent-read field `<plugin>.url` only the secrets of that plug-in are asked for. Instances from a settings source keep their own rule, and so does `mcp-tools`.

Instance names are lowercase letters, digits and hyphens, up to 20 characters, and are unique within a plug-in. Some names are reserved, and Admin refuses them with a sentence that names the name:

- `primary`, `all`, `no-data`, the names of core's own passes (`manual`, `slack-posts`, `selfreview`, `certs`, `jira-links` and `custom-checks`) and every check id and ran key of a plug-in that loaded, each alone or followed by a hyphen: with a check `tick`, neither `tick` nor `tick-east` can name an instance of any plug-in;
- within one plug-in, a name `n` when `<id>-<n>` is another check id or ran key of that plug-in. With twin checks `tick` and `tick-cache`, the names `cache` and `cache-*` are refused, so that each pass name belongs to one instance.

A stored instance whose name has become reserved is parked with a reason, and a reserved name in the environment or the config file stops the start.

An instance a settings source defines is edited at its source only in the fields the source owns, and Admin shows those read-only. When the source gives no secret for the instance, it owns none of its fields. When it gives a secret, it owns that secret, every address (`url` field) it gives a value for, and the addresses that secret is sent to: the plug-in's own `url` fields and the relay of a remote agent, which carry every secret of the instance, and for a secret of another plug-in's agent read also the address fields of that read. A field the source gives no value for and that carries none of its secrets, such as `elastic.logging-url` or `langfuse.url` on a Kubernetes agent instance whose source gives only the agent's URL and token, stays free: Admin and a plug-in migration can set it. Once the source gives a value for that field, it owns it again and its value wins.

A plug-in can limit how many instances it takes. Admin then refuses one more with a sentence that names the limit. Instances from the environment, the config file or a plug-in source take the slots first, and one beyond the limit, wherever it was added, is parked with the same sentence.

## Observability plug-ins

Besides Prometheus and Kubernetes, Skopax ships plug-ins for Capacity, Redis, Kafka, Grafana, Elasticsearch, Varnish, CSP reports, PMM, Cloudflare, Airbyte and Langfuse. Each does nothing until an admin adds an instance in Connect > Integrations. A check runs every 15 minutes unless its entry says otherwise, and its id is also its pass name in Admin and in `GET /api/state`. Rule sets are tuned in Admin > General, under the plug-in's name, by the ids given here. Links to a service's own pages (Kibana, Grafana, PMM dashboards, the Airbyte timeline) are added only for an `https://` address.

### Reading through another instance

Capacity, Redis and Kafka read through a Prometheus instance; Varnish and CSP read through an Elasticsearch instance. Each has a field, `prometheus` or `elasticsearch`, that lists that plug-in's instances, and you pick the one that holds the series or the logs. The reader uses that instance's address, credential and CA certificate and stores none of its own.

- The boxes under the chosen instance's "What it watches" do not stop a reader: with `prom` switched off on Prometheus `main`, Capacity, Redis and Kafka still read through `main`. To stop a reader, switch off its own check or its instance.
- If the chosen instance is switched off, deleted or parked, only the reader's pass fails, with a text that names that instance, and the reader's cards stay open until it reads again.
- Grafana and Kafka leave out what a Prometheus instance already watches, but only while that instance's `prom` check is on. Grafana skips the datasources whose URL is the URL of such an instance. Kafka does not count the pod restarts in the namespaces that the Prometheus instance it reads through already watches, while that instance's `kube` profile is also on. When that check is switched off, Grafana and Kafka raise those cards themselves, and when it is switched on again their copies close at their next pass.

### When a credential lacks a permission

A read that the service refuses (HTTP 401 or 403, or the service's own permission error) never counts as "nothing there".

- **The pass** stays ok for the parts it could read, and its status ends in `no access to <part>, <part>`. The cards of a refused part stay open, because the pass cannot tell whether they still hold.
- **A warn card** per instance and refused part names what to add: `<Plug-in> cannot read <part>: the <credential> lacks <permission>`. The credential is named as the plug-in calls it (token, password, API key, keys, application, client id and secret). When the service's documentation names no permission, the card says "the needed permission". For a 401 the card reads `the <credential> is invalid or lacks <permission>`, because an expired or revoked credential and a missing permission answer alike. With no credential set, the card says `<Plug-in> cannot read <part>: the server requires credentials and none are set`. Two checks of one plug-in that read the same refused part each raise their own card, and both close together.
- **The card closes** at the first pass that reads the part again, or once the part is switched off or left out by the instance's settings. A pass where the part failed for another reason, such as a timeout or an HTTP 5xx, keeps the card, so a flaky service never clears a real refusal.
- **Test connection** fails with a fixed sentence that names the part and the permission when a part the switched-on checks read answers 403; parts that only switched-off checks read are not tried. A 401 keeps the credentials text, except right after the plug-in's own login or token request succeeded with the same credentials, where it gives the `is invalid or lacks` sentence. With no credential set, a 401 says the service requires credentials and none are set.
- **A tool call** that is refused returns an error that names the missing permission, never an empty answer.
- **Connect > Integrations** shows an instance with a refused part as No access, a warning, not as healthy. A pass that failed stays an error.
- **On the board** every access card type (`no-access`, and the `access` and `umami-access` cards of the web, app and marketing plug-ins) shows under one No access type and group; the card keys keep their own types. The investigator never picks an access card for a run of its own; someone can still ask about one.
- **The core pass `jira-links`**, which reads through Jira, uses the same forms (`jira-links:access:issues`, `error: issues: <reason>`), and its card also closes when Jira or Jira links is switched off.

The permissions each plug-in needs are listed in its entry below.

### Quiet, capped and failed passes

An ok pass says `ok`, or `ok` and, after a middle dot, one or more of these tails, in this order: the `no data` forms, then the `read` forms, then `no access to`. A pass where one part failed says `error: <part>: <reason>` and may carry the same tails.

- `no data`: every part it reads answered with nothing, for example a Prometheus without redis_exporter series or an Elasticsearch without APM indices. The pass opens no card and closes none.
- `no data for <part>, <part>`: some parts answered with nothing. Their cards stay open; the other parts are judged as usual. A plug-in whose pass has a single ran key, such as Langfuse, cannot hold one part's cards apart, so its pass still closes them; Langfuse holds its turn cards when a set `turn-name` matches nothing.
- `read <n> of <m> <things>`: the read stopped at a cap. What was not read keeps its cards. `<things>` may name where they were read, such as `alerts in <datasource>`.
- `read <n> <things>, more not read`: the same, when the service gives no total.
- `no access to <part>, <part>`: see above.
- `browser down`: the Website browser service did not answer (see [The browser service](#the-browser-service)). Admin and the tile show it as a warning, browser down; a refused part beside it shows as No access.

When a part failed for another reason, the parts that were read still close their cards; the failed part's cards stay. Self-review, Admin and the page count it as failed. A check that reads logs and finds a field it needs missing from the index opens a warn card `<Plug-in> logs lack the field <name>`; the card closes at the first pass that finds it. Test connection of Varnish and CSP names the missing field too.

### Second instances

Each instance has its own cards. The primary's card keys and pass names carry no instance name, so they stay as they were when a second instance is added. Another instance's card keys end in `@<name>` and its pass names in `-<name>`: Capacity `east` runs the pass `capacity-east`, and its cards never close or replace the primary's.

### Reserved instance names

The check ids and ran keys of these plug-ins cannot name an instance of any plug-in, alone or followed by a hyphen: `prom`, `kube`, `alert`, `capacity`, `redis`, `kafka`, `grafana`, `nodes`, `logs`, `apm`, `logflow`, `varnish`, `varnish-cache`, `csp`, `pmm`, `qan`, `mongo`, `cf`, `edge`, `airbyte`, `langfuse`, `sonarqube` and `ghdeploy`. A Varnish instance also cannot be named `cache` or `cache-<anything>`. `no-data` is reserved for every plug-in.

### Prometheus

**Set it up:**

1. Enter the base address of the Prometheus API in `URL`, for example `https://prometheus.example.com`. Skopax reads `/api/v1`; the checks need series from Micrometer, KrakenD, ingress-nginx, kube-state-metrics or `kafka_exporter`, as set in `Profiles`.
2. Authentication `None`: nothing else to do. `Username and password`: Prometheus checks it only if started with `--web.config.file` holding `basic_auth_users` (bcrypt hashes); enter that user in `Username` and `Password`.
3. Authentication `Bearer token`: Prometheus does not check bearer tokens itself, so use it only behind a proxy that does; paste the token into `Token`. Press `Test connection` to see the Prometheus version.

Vendor guide: [Prometheus: basic authentication](https://prometheus.io/docs/guides/basic-auth/)

- **Fields:** `url`, `ui-url` (the address people open for graph links), `auth` (`none`, `bearer` with `token`, or `basic` with `username` and `password`), `ca`, `namespaces` (a pattern, default `.*`) and `profiles`.
- **Profiles** pick the metric families the check reads, as a comma list: `spring` (Micrometer HTTP, log and Hikari metrics), `krakend` (KrakenD gateway), `ingress` (ingress-nginx), `kube` (kube-state-metrics restarts, pod states, nodes and image changes) and `kafka` (consumer lag). All five are on by default, and an empty list means all five. Alerts are always read. Switching `kube` off also stops image-change detection, so Releases, the deploy rows and the warm-up after a deploy get nothing from this instance.
- **Ingress exclusion pattern** (`ingress-exclude`, a regular expression) leaves out ingresses whose name matches it from the `ingress` profile. Empty, the default, leaves none out.
- **Check** `prom` (ran keys `prom`, `kube`, `alert`; sections `services`, `deploys`, `routes`). **Tool** `promql`. **Rules** `prometheus.rates`. Its tile is on the Services page.
- **Access:** the token or password must be allowed to query the API. A refused read is the part `metrics`.

### Capacity

Volume, autoscaler, JVM heap and host disk headroom, read through a Prometheus instance.

**Set it up:**

1. No credential of its own: Capacity reads through a Prometheus instance. Add that Prometheus instance first and pick it in `Prometheus`; it signs in as set there.
2. That Prometheus must hold `kubelet_volume_stats_*` (volumes), `kube_horizontalpodautoscaler_*` (autoscalers), `jvm_gc_live_data_size_bytes` and `jvm_gc_max_data_size_bytes` (JVM heap) and node_exporter `node_filesystem_*` (host disks). A part with no series gives no data.
3. Host disks of machines Prometheus does not scrape: add the PMM instance first and pick it in `Host disks from PMM`.

Vendor guide: [Prometheus: HTTP API](https://prometheus.io/docs/prometheus/latest/querying/api/)

- **Fields:** `prometheus`; `jvm` (default on) reads the heap of Micrometer services; `host-disks` (default on) reads the disks that node_exporter reports in `node_filesystem_*` series; `volume-selector` adds label matchers to the `kubelet_volume_stats_*` series (empty by default); `fs-selector` holds the `node_filesystem_*` matchers, by default leaving out pseudo file systems and container mounts.
- Switching `jvm` or `host-disks` off closes that part's cards at the next pass. On a Kubernetes Prometheus, node root disks are usually covered by the cluster's own DiskPressure checks, so you may switch `host-disks` off there.
- **Host disks from PMM:** `disks-pmm` names a PMM instance whose metrics data source holds the `node_filesystem_*` series of hosts that Prometheus does not scrape. The disks are then read from there, with the same `fs-selector`, and everything else still from Prometheus. Empty (the default) reads them from Prometheus. A PMM that cannot be read fails only the host disks part, with the reason in the status; a refusal opens the access card for `dbdisk`, naming the PMM token. `host-disks` off ignores it. Test connection also asks that PMM.
- **Check** `capacity` (section `capacity`). **Rules** `capacity.rules`. **Pages** Disk, JVM and Capacity.
- **Access:** reads with the Prometheus instance's credential. Refused parts are `volumes`, `hpa`, `heap` and `dbdisk`.

### Redis

Redis instances from redis_exporter and the Redis calls of each service from Lettuce client series.

**Set it up:**

1. No credential of its own: Redis reads through a Prometheus instance. Add that Prometheus instance first and pick it in `Prometheus`; it signs in as set there.
2. That Prometheus must hold the redis_exporter series (`redis_up`, `redis_memory_used_bytes` and others) and, for the Redis calls of each service, the `lettuce_seconds_*` series of the Lettuce client. Use `Selector` to narrow what is read.

Vendor guide: [redis_exporter](https://github.com/oliver006/redis_exporter)

- **Fields:** `prometheus`; `selector`, label matchers added to every Redis query, such as `env="prod"` (empty reads every series).
- **Friendly names:** `peer-names` holds comma-separated `address=name` entries, such as `10.0.4.12:6379=sessions-cache, 10.0.4.13=queue`. The address a service calls, which Lettuce reports as `net_sock_peer_addr`, is shown as the name on the Services table, on cards and in the tool; an entry without a port matches any port. Empty shows the address. Test connection refuses an entry it cannot read, and a duplicate address keeps its first entry.
- **Check** `redis` (section `redis`). **Tool** `redis`. **Rules** `redis.rules`. **Page** Redis.
- **Access:** reads with the Prometheus instance's credential. Refused parts are `exporter` and `app`.

### Kafka

Brokers, partitions, consumer groups and the Kafka calls of each service, from kafka_exporter and Spring Kafka series.

**Set it up:**

1. No credential of its own: Kafka reads through a Prometheus instance. Add that Prometheus instance first and pick it in `Prometheus`; it signs in as set there.
2. That Prometheus must hold the kafka_exporter series (`kafka_consumergroup_lag_sum`, `kafka_brokers` and others) and, for the calls of each service, the `spring_kafka_*` timers. Use `Preset` strimzi for Strimzi clusters, otherwise kafka-exporter.

Vendor guide: [kafka_exporter](https://github.com/danielqsj/kafka_exporter)

- **Fields:** `prometheus`; `preset`, `kafka-exporter` (default; clusters grouped by the exporter job) or `strimzi` (clusters grouped by namespace, with broker disks and pod restarts); `namespaces`, a pattern for the namespaces of the Kafka clusters (default `.*`). A preset changes only label and metric names, never thresholds. Under `kafka-exporter` the status adds that broker disks need the `strimzi` preset.
- **Check** `kafka` (section `kafka`). **Tool** `kafka`. **Rules** `kafka.rules`. **Page** Kafka.
- **Access:** reads with the Prometheus instance's credential. Refused parts are `clusters`, `groups` and `app`.

### Grafana

Alerts firing on the Prometheus datasources of Grafana.

**Set it up:**

1. In Grafana open Administration > Users and access > Service accounts > Add service account. Give it a display name and the Viewer role, then Create.
2. Open the new service account and choose Add service account token. Name it, leave the expiry as you prefer, and choose Generate token.
3. Paste the token into `Token`. It needs `datasources:read` and `datasources:query` on each Prometheus datasource and `alert.rules:read`. Viewer alone may not reach every datasource; if `Test connection` or a card names a missing right, add it by role or datasource permission.
4. Enter the Grafana address in `URL` (also used for links to alert rules). Press `Test connection`.

Vendor guide: [Grafana: service accounts](https://grafana.com/docs/grafana/latest/administration/service-accounts/)

- **Fields:** `url` (also used for links to the alert rules), `token` (a service account token), `ca` and `skip-datasources` (comma-separated datasource names). Grafana reads at most 12 datasources a pass and says `read <n> of <m> datasources` past that. A datasource it stops watching, because it is skipped or because a Prometheus instance now covers it, has its cards closed at the next pass.
- **Check** `grafana` (sub-keys `grafana:<datasource>`; section `grafanaAlerts`). **Tool** `grafana_alerts`.
- **Access:** `datasources:read` to list the datasources (part `datasources`), `datasources:query` on each datasource read (the part is its name) and, for the Grafana-managed alerts the tool shows, `alert.rules:read`.

### Kubernetes nodes

The Kubernetes plug-in's node and agent heartbeat.

**Set it up:**

1. Mode `This cluster`: Skopax reads with the service account of its own pod. Bind that account to a ClusterRole that allows get and list on nodes and pods, get on `pods/log`, list on events, get on deployments and daemonsets and list on replicasets.
2. A Role per namespace covers pods, events and rollouts, but the node checks and the node read need nodes, and pods and events in all namespaces, so those stay in a ClusterRole.
3. Mode `Through an agent`: run the agent image in the other cluster with `SKOPAX_MODE=agent`, `SKOPAX_AGENT_TOKEN` and a service account with the same permissions. Enter the agent address in `URL` and its token in `Token`.
4. `Relay` (advanced): name another agent instance that passes the calls to this agent. Press `Test connection`.

Vendor guide: [Kubernetes: using RBAC authorization](https://kubernetes.io/docs/reference/access-authn-authz/rbac/)

Example RBAC for mode `This cluster` (and for an agent's service account); change the names and namespace to yours:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: skopax-reader
rules:
  - apiGroups: [""]
    resources: ["nodes", "pods"]
    verbs: ["get", "list"]
  - apiGroups: [""]
    resources: ["pods/log"]
    verbs: ["get"]
  - apiGroups: [""]
    resources: ["events"]
    verbs: ["list"]
  - apiGroups: ["apps"]
    resources: ["deployments", "daemonsets"]
    verbs: ["get"]
  - apiGroups: ["apps"]
    resources: ["replicasets"]
    verbs: ["list"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: skopax-reader
subjects:
  - kind: ServiceAccount
    name: skopax
    namespace: skopax
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: skopax-reader
```

- **Check** `nodes`, every 5 minutes, runs once, on the primary Kubernetes instance, and reaches every Kubernetes instance from there, so its keys are `nodes:<agent>` whichever instance is primary. Every Kubernetes instance that is switched on is pinged, whatever its own "What it watches" says; switch the instance off to stop it. Switching an agent off, deleting it, parking it or moving it to `in-cluster` closes its node card at the next pass. Turned back on, it starts like a new agent: an agent that has never answered gets its card after 24 hours, one that answered before after 5 minutes down. Section `nodes`; page Nodes.
- **Access:** `list on nodes` in each cluster (part `nodes of <instance>`). An in-cluster primary raises the warn card; an agent whose own cluster read is refused shows `cluster not read` on its Nodes row instead of a card.
- **Local instance:** inside a cluster the environment adds the in-cluster primary `local`; `SKOPAX_KUBE_LOCAL_NAME` names it otherwise (see [Agent mode](operations.md#agent-mode)).

### Elasticsearch

New and growing error and warning log patterns, APM latency and failures, and the log flow per namespace, read with ES|QL.

**Set it up:**

1. In Kibana open the API keys page, choose Create API key and pick a user key. Creating it needs the `manage_own_api_key` or `manage_api_key` privilege.
2. Under access configuration use custom privileges: read on the `Log index` (default logs-*), the `APM index` (default traces-apm*) and the `Log flow index` if you set one. Add the cluster privilege monitor if you want `Test connection` to show the version.
3. Copy the encoded key and paste it into `API key`. If the APM data is in another cluster, enter its address in `APM URL` and its own key in `APM API key`; empty means the log cluster and its key.
4. Enter the cluster address in `URL`. A cluster without security needs no key. Press `Test connection`.

Vendor guide: [Elastic: API keys](https://www.elastic.co/docs/deploy-manage/api-keys/elasticsearch-api-keys)

- **Fields:** `url` and `api-key` for the log cluster; `apm-url` and `apm-api-key` for the APM cluster, each defaulting to the log cluster's; `ca`; `ui-url` (Kibana) and `data-view` for the Discover links on cards and rows.
- **Log fields** default to ECS names: `log-index` `logs-*`, `apm-index` `traces-apm*`, `namespace-field` `kubernetes.namespace`, `service-field` `service.name`, `level-field` `log.level`, `message-field` `message` and `error-field` `error.message`. When the log index has not mapped the error field, the logs check reads the message only and Test connection says so.
- **`index-namespace`:** for setups with one index per namespace, a pattern with a group named `ns` takes the namespace from the index name, for example `logs-(?P<ns>[a-z0-9-]+)-.*` (empty by default, which reads `namespace-field`).
- **Options on the checks**, each empty by default, and an empty option changes nothing:
  - `logflow-index` is the index the log flow check reads; empty reads `log-index`. Test connection reads each index its checks need and names the one the key cannot read.
  - `message-mask` is a pattern whose matches are replaced by `<masked>` in the first 400 characters of a log line before the logs check groups lines, so a request body or an id no longer makes every line its own pattern. Elasticsearch runs it (Java syntax) and Test connection tries it there. A pattern that cannot be read, that matches an empty text or that backtracks badly on a test string is left out and Test connection says so; the check keeps running unmasked. A mask that passes that check but that Elasticsearch rejects fails the logs pass, and its status names `message-mask`.
  - `apm-p95-overrides` holds comma-separated `service=times:floor_ms` entries, such as `checkout=3:500, search=4, batch=:1000`, that replace the factor and the floor of the `elasticsearch.apm` rule set for one service; a missing part keeps the rule set value. The query reads with the loosest value and each service is judged by its own. Test connection names the entries it ignores.
  - `discover-columns` lists extra log fields, at most 10, that every Discover link shows after the level, service, message and error columns.
- **Checks** `logs` (section `logTop`), `apm`, and `logflow`, which runs every 10 minutes (section `logflow`). **Tool** `esql`, which also serves agent reads; its `cluster` input is `logs` or `apm`. Each query waits at most 55 seconds, so a query that times out over more than one day and its one retry on the last day both fit in the tool's 120 seconds; the narrowed answer carries its note above the rows and is cut to 200 rows like any other answer. **Rules** `elasticsearch.apm` and `elasticsearch.logflow`. **Page** Logs. With a Kibana URL set, cards about a service gain a link to its logs, and deploy reports a Logs link over the deploy window.
- **Access:** the read privilege on the log index (logs and log flow) and on the APM index (APM). The cluster's `monitor` privilege is optional; without it Test connection shows no version. Refused parts are `logs` and `apm`.

### Varnish

The Varnish cache hit ratio against the day and the week before, and a daily list of the paths that miss the cache most, from Varnish logs in Elasticsearch.

**Set it up:**

1. No credential of its own: Varnish reads logs through an Elasticsearch instance. Add that instance first and pick it in `Elasticsearch`; its `API key` needs read on the Varnish index.
2. Set `Index` if the Varnish logs are not in that instance's log index. The log lines need the fields `varnish.method`, `varnish.cache_status` and `varnish.path` (keyword); `Test connection` names any that are missing.

Vendor guide: [Elastic: API keys](https://www.elastic.co/docs/deploy-manage/api-keys/elasticsearch-api-keys)

- **Fields:** `elasticsearch`; `index` (empty means that instance's log index); `namespace` (empty: the cards show `-`) and `service` (default `varnish`) for the cards; `personal-paths`, a pattern for sign-in or checkout flows whose misses are expected.
- **Log fields:** `varnish.method.keyword`, `varnish.cache_status.keyword` and `varnish.path.keyword` are required; `varnish.query_string`, `varnish.query_params._rsc.keyword` and `varnish.response_code.keyword` add hints when present.
- **Checks** `varnish` (section `varnish`) and `varnish-cache`, once a day (section `varnishCache`). **Rules** `varnish.rules`.
- **Access:** the read privilege on the Varnish index, with the Elasticsearch instance's API key (part `logs`).

### CSP reports

Content Security Policy violation reports in the logs: hosts the browsers block for the first time, and jumps in the hourly count.

**Deprecated:** use the [CSP preset](custom-checks.md#csp-reports) under custom checks; this plug-in leaves in 0.4.

**Set it up:**

1. No credential of its own: CSP reports reads logs through an Elasticsearch instance. Add that instance first and pick it in `Elasticsearch`; its `API key` needs read on the indices the query names.
2. Write `Query` as ES|QL that selects the report lines, such as `FROM logs-* | WHERE message LIKE "*csp-report*"`. Each report is read from the message column. Press `Test connection`.

Vendor guide: [Elastic: ES|QL](https://www.elastic.co/docs/reference/query-languages/esql)

- **Fields:** `elasticsearch`; `query`, the ES|QL source and filter that select the report lines, such as `FROM logs-* | WHERE message LIKE "*csp-report*"`; `namespace` (empty: the cards show `-`) and `service` (default `csp`) for the cards.
- **Check** `csp` (section `csp`). **Rules** `csp.rules`.
- **Access:** the read privilege on the indices the query names (part `reports`).

### PMM

Postgres servers, slow queries and MongoDB members, read from Percona Monitoring and Management.

**Set it up:**

1. In PMM open the Grafana service accounts page (Administration > Users and access > Service accounts), add a service account with the Viewer role and generate a token for it.
2. Paste the token into `Token` and the PMM address into `URL`. Viewer can query the metrics and Query Analytics data sources.
3. Leave `Metrics data source` and `QAN data source` empty only if the token may list data sources (the Admin role or `datasources:read`). Otherwise enter the data source uids so Skopax need not list them.

Vendor guide: [PMM: service accounts and authentication](https://docs.percona.com/percona-monitoring-and-management/3/api/authentication.html)

- **Fields:** `url` (also used for dashboard links), `token`, `ca`, `metrics-datasource` and `qan-datasource` (data source uids), `postgres-services` and `mongo-services` (patterns), `replica-set` and `exclude-users` (default `pmm`).
- **CA certificate:** a PMM with its own certificate authority is verified against the `ca` field. A wrong CA fails Test connection with the TLS sentence. `ca-pinned` (Trust this certificate as it is), shown once `ca` is set, is for a server whose own certificate fails the usual checks: paste the server's own certificate; with the switch on, only that exact certificate is accepted, even if it is expired, self-signed or names another host. The chain, name and date checks are then off, and every other certificate is refused, also one signed by a certificate in `ca`, so a CA certificate does not work with the switch on. A pasted bundle of server certificates accepts each of them. With `ca` empty the switch has no effect, and saving with `ca` empty drops it. A new `ca`, or switching `ca-pinned` off, asks for the token again (see [Instances](#instances)). PMM, Elasticsearch, Prometheus, Grafana, Airbyte, Langfuse, GitLab, Argo CD and Umami all have it.
- **Discovery:** with a data source uid empty, PMM uses the first Prometheus data source (metrics) or ClickHouse data source (Query Analytics) it lists at run time. Test connection names what it found, so you can pin it.
- **Options**, each empty by default, and an empty option changes nothing:
  - `split-users` lists database users, such as a reporting user, that are read apart from the application. Their connections no longer count towards the connection jump card of the server (every user still counts towards saturation) and raise their own `pg-conn-split` info card; their queries leave the Query Analytics pass and are read in a second pass whose cards are info only and keyed `...|split`. A user also listed in `exclude-users` stays ignored. Connections are split by the `usename` label of `pg_stat_activity_count`.
  - `qan-app` is a pattern with a group named `app` that takes the application name of a Query Analytics card from the database name, for example `^(?P<app>.+)_db$`. A database it does not match, or a pattern that is slow, keeps the database name. Test connection refuses a pattern without the group.
  - `host-header` is sent as the Host header with every request, for a PMM behind a proxy that routes by host name (a host name with an optional port).
  - `link-base` is the address the dashboard links on cards open instead of `url`, for example a public address for a PMM that Skopax reaches by an internal one (https only; it is only shown, never read, and a value that is not an address is refused when saved; editing it asks for the token again, as for the URL).
- **Checks** `pmm` (Postgres servers), `qan` (Postgres queries) and `mongo` (MongoDB members), sections of the same names. **Tools** `pmm_query` and `qan`. **Rules** `pmm.postgres`, `pmm.qan` and `pmm.mongo`. **Pages** Postgres and MongoDB.
- **Access:** the Viewer role to query the metrics and QAN data sources (parts `metrics` and `QAN`); listing data sources, needed only while a uid is empty, needs the Admin role or `datasources:read` (part `data sources`).

### Cloudflare

Health checks, certificate packs, status-page components and the edge 5xx share of Cloudflare zones.

**Set it up:**

1. In the Cloudflare dashboard open My Profile, API Tokens and choose Create Token (an account owned token under Manage Account, API Tokens works too).
2. Start from scratch with a custom token and name it, for example Skopax read-only.
3. Add a Zone policy with four Read permissions, each picked as Zone, name, Read: Zone, Zone, Read; Zone, Analytics, Read; Zone, SSL and Certificates, Read; Zone, Health Checks, Read.
4. Set Zone Resources to the zones you will list in Zones (or all zones of the account). Add no Edit permission.
5. Continue to summary, then Create Token, and copy the secret: Cloudflare shows it once.
6. Paste it into API token, then list the zone names in Zones. Test connection reports which permission is missing.

Vendor guide: [Cloudflare: create an API token](https://developers.cloudflare.com/fundamentals/api/get-started/create-token/)

- **Fields:** `token`, `zones` (comma-separated zone names; their ids are looked up), `account` (empty means the account of the first zone) and `status-components` (semicolon-separated, written as the status page shows them). The status-page check runs only when components are set.
- **Pickers:** `zones` and `status-components` are searchable lists in the form. The values you chose show as chips with a remove button, and the search box under them filters the list as you type. `status-components` lists every component and group name of the public status page summary, exactly as the check matches them, and needs no token. `zones` lists the zones the API token can see (the token typed in the form, or the saved one of the instance you edit); without a token it says "Enter the API token first". It reads at most 1000 zones (20 pages) within the 10 seconds a list may take, and says under the list how many zones it listed when it stops early. You can still type a name that is not in the list and press Enter (or Save or Test connection); it is kept and marked "not in the list". A typed name that the list has in another case is stored as the list spells it, which is how the status check matches it. The saved value stays the same comma- or semicolon-separated text.
- **Checks per zone:** `zone-checks` holds comma-separated `zone=checks` entries, where checks is `cf` (health checks and certificates), `edge` (edge 5xx), `cf+edge` or `none`, such as `example.org=edge, example.net=none`. A zone that is not listed runs both. The cards of a check a zone no longer runs close at the next pass. The status components are not per zone. Test connection refuses an entry it cannot read, a mix of `none` with a check, and a zone that is not in `zones`.
- **Checks** `cf` (health checks, certificates and status; sub-keys `cf:<zone>:checks` and `cf:<zone>:certs`; section `cloudflare`) and `edge` (sub-keys `edge:<zone>`; section `edge`). **Tools** `edge` and `cloudflare_graphql`. **Rules** `cloudflare.edge`. **Page** Cloudflare.
- **Access:** a read-only API token with Zone Read (part `zones`), Health Checks Read (`health checks`), SSL and Certificates Read (`certificates`) and Analytics Read (`analytics`) on the watched zones.

### Airbyte

Failed and unusually long Airbyte sync jobs, from the Airbyte public API.

**Set it up:**

1. In Airbyte open your user menu > User settings > Applications. In Airbyte Cloud choose Create an application, name it and Submit; on Core the default application is already there. Reveal the client secret.
2. Copy the client id into `Client id` and the secret into `Client secret`. An application has the permissions of the user it belongs to, so use a user who can read the workspaces to watch.
3. Enter the address of the Airbyte web app in `URL`; Skopax calls its public API under `/api/public/v1`. Press `Test connection`.

Vendor guide: [Airbyte: configuring API access](https://docs.airbyte.com/platform/using-airbyte/configuring-api-access)

- **Fields:** `url`, `client-id` and `client-secret` of an Airbyte application, `connections` (comma-separated names or ids; empty watches every connection) and `ca`. At most 500 connections are read, and the status says so past that.
- **Connections list:** an entry in `connections` that names no connection makes the status say `no data for <entry>` and holds the open cards of a connection with that name or id, so a renamed connection keeps its cards until the entry is fixed.
- **Options**, each empty by default, and an empty option changes nothing:
  - `connection-match` is a pattern that also watches every connection whose name it matches, on top of `connections`; with `connections` empty only the matching connections are watched. A pattern that cannot be read fails the pass with a fixed reason, and a slow one matches nothing.
  - `severities` holds comma-separated `name=severity` entries (a connection name or id, and `info`, `warn` or `crit`), such as `nightly-orders=crit, test-sync=info`. Other connections stay `warn`. Test connection refuses an entry it cannot read.
  - `workspaces` holds comma-separated workspace ids (case is ignored); only the connections of these workspaces are watched, and a name listed in `connections` that lives elsewhere is left out, not reported as missing.
- **Check** `airbyte` (sub-keys `airbyte:<connection id>`; section `airbyte`). **Tool** `airbyte`. **Rules** `airbyte.rules`. **Page** Airbyte.
- **Access:** an application reads only the workspaces, connections and jobs its owning user can access. Refused parts are `connections` and `jobs`.

### Langfuse

Errors, traffic, latency and cost of the turns an AI app traces to Langfuse.

**Set it up:**

1. In Langfuse open the project and go to Settings > API Keys, then Create new API keys. Name the key and under Permissions choose Viewer (read-only); Skopax only reads. A key cannot be changed later.
2. Copy the public key into `Public key` and the secret key into `Secret key`. The secret key is shown only once.
3. Leave `URL` as it is for Langfuse Cloud, or enter the address of your own server. Press `Test connection`.

Vendor guide: [Langfuse: roles and project API key permissions](https://langfuse.com/docs/administration/rbac)

- **Fields:** `url` (default `https://cloud.langfuse.com`), `public-key` and `secret-key` of the project, `environment` (empty reads every one), `namespace` (empty: the cards show `-`) and `service` (default `langfuse`) for the cards, `turn-name` and `ca`. With `turn-name` empty, a turn is the root observation of each trace, which needs a Langfuse version that knows `isRootObservation`; on an older server, set `turn-name` to the name of the observation that is one turn.
- **Check** `langfuse` (section `langfuseCheck`). **Tool** `langfuse`, which also serves agent reads. **Rules** `langfuse.rules`.
- **Access:** a project key pair with read access to the project. Refused parts are `metrics` and `observations`.

### Spring Cloud Config

Settings and instances read from a Spring Cloud Config server; see [Spring Cloud Config](configuration.md#spring-cloud-config) for every variable.

**Set it up:**

1. This plug-in has no form. Set `SPRING_CONFIG_URL` to the config server address and `SPRING_CONFIG_APP` to the application name; `SPRING_CONFIG_PROFILE` and `SPRING_CONFIG_LABEL` are optional.
2. If the server asks for a sign-in, set `SPRING_CONFIG_USER` and `SPRING_CONFIG_PASSWORD` together. Set `SPRING_CONFIG_MAP` to the comma list of config key=Skopax target entries to read, then restart Skopax.

Vendor guide: [Spring Cloud Config Server](https://docs.spring.io/spring-cloud-config/reference/server.html)

## Code, tickets and chat

GitLab, GitHub, Jira, Confluence, SharePoint and OneDrive, Argo CD, SonarQube, Slack and Microsoft Teams ship with Skopax and do nothing until they are set up: open Connect > Integrations, choose "+ Add integration" and the plug-in, fill in its fields, choose Test connection, then Save & turn on. GitLab, GitHub, Jira, Confluence and Slack take one connection each, and Admin refuses a second one. Argo CD takes one instance per Argo CD, SharePoint and OneDrive one per folder, and Teams one instance per route. A token changed in Admin is used by the next call with no restart; only the Slack app token needs one (see [Slack](#slack)). A read refused for a missing permission follows [When a credential lacks a permission](#when-a-credential-lacks-a-permission). Argo CD's check id `argo` is a reserved instance name in the same way as the observability check ids (see [Reserved instance names](#reserved-instance-names)), so no instance of any plug-in can be named `argo` or `argo-<anything>`.

GitLab, Jira, Confluence and Slack can also take their connection from the environment; GitHub is set up in Admin only, since CI runners such as GitHub Actions set `GITHUB_*` variables of their own. Each set defines an instance named `main`, shown in Admin and edited in the environment:

- GitLab: `GITLAB_TOKEN` and `GITLAB_GROUP`, both needed, and `GITLAB_URL` (default `https://gitlab.com`);
- Jira: `JIRA_URL`, `JIRA_EMAIL` and `JIRA_API_TOKEN`, all three needed, and `JIRA_CLOUD_ID`;
- Confluence: `CONFLUENCE_URL`, `CONFLUENCE_EMAIL` and `CONFLUENCE_API_TOKEN`, all three needed, and `CONFLUENCE_SPACES`;
- Slack: `SLACK_BOT_TOKEN`, and `SLACK_APP_TOKEN` for the listener.

A set with some of its needed variables but not all gives no instance: Skopax starts, and one log warning names the missing variables. An optional variable alone, such as `GITLAB_URL`, gives no instance and no warning.

A connection is always an instance, never a setting. The keys `gitlab.token`, `gitlab.group`, `jira.url`, `jira.email`, `jira.api-token`, `jira.cloud-id`, `slack.bot-token` and `slack.app-token` in the config file or a settings source are not read, and Skopax logs one warning at start that names the keys found and the instance form to use: give the connection as an instance under `plugins:` (see [Settings sources](configuration.md#settings-sources)). The behaviour settings, `SLACK_CHANNEL` (`slack.channel`) among them, are in Admin > General. Without a serving instance, what needs the integration is off: with no Jira, Create task is refused with a sentence and ticket keys are not linked, whatever other Jira settings are set.

### GitLab

**Set it up:**

1. In GitLab choose your avatar > Edit profile > Access > Personal access tokens, then Generate token > Legacy token. Or, as a group Owner, open the group > Settings > Access tokens > Add new token.
2. Name it, set an expiration date, and select only the read_api scope. For a group token choose a role that can read the projects, such as Reporter.
3. Create the token, copy it at once (GitLab shows it once) and paste it into `Token`.
4. In `URL` keep `https://gitlab.com` or give your GitLab address. Enter the path of the group whose projects Skopax reads in `Group`, such as acme, then choose Test connection.

Vendor guide: [GitLab: personal access tokens](https://docs.gitlab.com/user/profile/personal_access_tokens/)

- **Fields:** `url` (default `https://gitlab.com`; the API is read under `/api/v4`), `token` (an access token with the `read_api` scope), `group` (the path of the group whose projects are read), `ca` and `ca-pinned` (as for PMM).
- **What uses it:** deploy enrichment (the tickets, commits and authors of each deploy on Releases), the code links the investigator writes, which must point under the group, and the read-only tools `gitlab_search`, `gitlab_file`, `gitlab_blame`, `gitlab_compare`, `gitlab_mrs` and `gitlab_pipelines`. The tools take a project's name or its full path at any depth; the group may be a subgroup path such as `acme/platform`. Deploy enrichment finds a project by the service name one subgroup below the group (`<group>/<subgroup>/<service>`), or deeper when no project of that name sits there; a project right under the group is not matched, and a deploy with no such project says so. While GitLab is off, deploy enrichment is skipped with one log line, and Releases says "not read: GitLab is off" in the Tickets cell of a deploy with no tickets. With GitLab on, a deploy not read yet says "not read from GitLab yet" during the 7 days that enrichment reads it, and "not read from GitLab" after that.
- **Access:** reads need the `read_api` scope or a role that can read the projects, such as Reporter. Test connection reads `/version`, then the group's projects. A refused read names the part (`projects`, `code`, `merge requests` or `pipelines`) and the permission. GitLab answers 404 for a group or a private project the token cannot see, so a 404 is worded as "no such item, or the token lacks" the permission. A read refused during deploy enrichment shows on Releases as "not read:" and the reason. A refused project marks only its own deploys, named with its path, and the sweep goes on; a refused group listing stops the sweep and marks every deploy it left. The mark stays until a later read of that deploy succeeds, and a deploy older than the 7-day enrichment window is not read again.

### GitHub

GitHub.com, or a GitHub Enterprise Server through the `url` field, read with a fine-grained personal access token.

**Set it up:**

1. In GitHub open your profile menu > Settings > Developer settings > Personal access tokens > Fine-grained tokens, then Generate new token. Name it and set an expiration.
2. Under Resource owner pick the organization or user you will enter as `Owner`. Under Repository access choose Only select repositories (or All repositories) for the repositories Skopax should read.
3. Under Repository permissions set Metadata, Contents, Pull requests, Checks, Commit statuses and Deployments to Read-only. Leave every other permission at No access.
4. Choose Generate token, copy it, and paste it into `Token`. If the organization requires approval, an owner must approve it first.
5. In `URL` keep `https://github.com`, or give the address of your GitHub Enterprise Server. Enter the organization or user in `Owner`, then choose Test connection.

Vendor guide: [GitHub: managing personal access tokens](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens)

- **Fields:** `url` (default `https://github.com`, whose API is `https://api.github.com`, also for `https://www.github.com`; GitHub Enterprise Cloud with data residency, `https://<name>.ghe.com`, is read at `https://api.<name>.ghe.com`; any other address is a GitHub Enterprise Server, read under `<url>/api/v3`, and its GraphQL under `<url>/api/graphql`), `token` (a fine-grained personal access token), `owner` (the organization or user whose repositories are read), `repos` (optional: the repositories whose deployments and releases the `github` check tracks, by name and comma separated, at most 20), `docs` (optional: a repository of the owner with an optional folder, such as `acme/handbook/runbooks`, read as company knowledge), `ca` and `ca-pinned` (as for PMM).
- **What uses it:** deploy enrichment (the tickets, commits and authors of each deploy on Releases), the code links the investigator writes, which must point under the owner on the `url` host, and the read-only tools `github_search`, `github_file`, `github_blame`, `github_compare`, `github_prs` and `github_checks`, with GitHub's notes for the investigator. The tools take a repository's name or `owner/name`; repositories of other owners are not read, and a search that names its own scope (`repo:`, `org:`, `user:` and the like) or holds a backslash is refused; pull request search results of another repository are dropped. Deploy enrichment finds the repository by the service name under the owner. `github_checks` reads the check runs (GitHub Actions jobs among them) and the commit statuses of one commit; `github_prs` tells merged from closed without merging. Blame is read through GitHub's GraphQL API, since its REST API has none.
- **Deployments and releases:** with `repos` set, the check `ghdeploy` (section `github`) reads, every 5 minutes, the last 10 deployments of each tracked repository with their latest state and its last 5 published releases. They are listed beside the deploys under Releases, in `GET /api/deploys`, in `deploys_since` and in `ctx.deploys`, as rows of kind `github`: a deployment as `<previous sha> -> <sha>` of its environment, a release as `<previous tag> -> <tag>`. A deployment's state is read until it is final (`success`, `failure`, `error` or `inactive`), at most 5 state reads per repository and pass. When the newest deployment of an environment ended `failure` or `error` in the last 24 hours, the warn card `github-deploy|github/<repo>|<environment>` names it, with links to its commit, its pull request (the one GitHub names for the commit) and the repository's deployments; it closes once a newer deployment of that environment succeeds or the 24 hours pass. A repository that is not found or not visible is named in the check's status.
- **Company knowledge:** the plug-in is a knowledge source for each instance whose `docs` field is set (`company_knowledge='docs'`, `knowledge_also=True`): its tile carries the Knowledge badge. With `docs` set, the tool `github_docs` searches and reads the Markdown and text documents (`.md`, `.markdown`, `.mdx`, `.txt`, `.rst`, `.adoc`) in that folder of that repository, and nothing outside it. Passwords, keys, tokens and the other masked values are masked in what it returns. Without `docs` the tool is not offered.
- **Access:** give the fine-grained token read-only repository permissions: Metadata, Contents, Pull requests, Checks, Commit statuses and Deployments, all Read, on the repositories to read. Test connection reads `/user`, the owner (`/users/<owner>`), the first page of the repositories the token can see ("100 or more" past one page), and on a GitHub Enterprise Server its version from `/meta`. A refused read names the part (`repositories`, `code`, `pull requests`, `checks`, `deployments` or `releases`) and the permission. GitHub answers 404 for a private repository the token cannot see, so a 404 is worded as "no such repository, or the token cannot see it". A renamed repository answers with a redirect, which the transport follows itself; when the redirect leads to another host, the transport drops every header, the token among them, before following it. On a transport that does not follow redirects, the plug-in follows one redirect on the API host and no other. A refused read in the `ghdeploy` check raises the access card `ghdeploy:access:<part>`, and a repository that answers 404 mid-pass is named in the status while the pass goes on. The check id is `ghdeploy`, not `github`, so it reserves no instance name a GitHub MCP server is likely to take (see [Reserved instance names](#reserved-instance-names)).
- **Rate limits:** GitHub's primary limit (HTTP 403 or 429 with `x-ratelimit-remaining: 0`) and its secondary limit (HTTP 403 or 429, with or without `Retry-After`) are never read as a missing permission. Every read of that instance then pauses for the time GitHub gives (`Retry-After`, or until `x-ratelimit-reset`; 60 seconds when it gives none; at most an hour) and fails at once with "GitHub rate limit reached: reads pause for N s", without calling GitHub. Deploy enrichment stops that sweep and tries again on the next one.
- **Both GitLab and GitHub:** both can serve at once; each keeps its own instance. Deploy enrichment, own commits and the investigator's code links ask GitLab first and GitHub second, and a repository resolves on the first one that knows it. Precedence is strict: while GitLab cannot answer (down, refused or rate limited), a repository is undecided and GitHub is not asked; deploy enrichment then leaves that one deploy unmarked for a later sweep and goes on with the others, and so it does when GitHub cannot answer for a repository GitLab does not hold. A host that could not answer is not asked again for a minute. A refusal from one host never marks a deploy another host holds. A repository found on one host is read only there, also after the other host goes off. Each host rules on the links of its own host only. While only one of them serves, it works as if it were alone.

### Jira

Jira Cloud only: calls go through the Atlassian API gateway, `api.atlassian.com`, with an account's email and API token. Jira is also a company knowledge source (`company_knowledge='always'`, `knowledge_also=True`): the investigator searches and cites its issues, and its tile carries the Knowledge badge.

**Set it up:**

1. Sign in as the account Skopax will use and open `https://id.atlassian.com/manage-profile/security/api-tokens`. Choose Create API token with scopes, name it and set an expiry (1 to 365 days).
2. Pick Jira as the app and select the scopes read:jira-work and read:jira-user. The account also needs Browse projects and Browse users and groups in Jira.
3. Create the token, copy it and paste it into `API token`. Enter that account's address in `Email`.
4. In `Site` give your Jira Cloud address, such as `https://example.atlassian.net`. Leave `Cloud id` empty unless asked, then choose Test connection.
5. A token made with the plain Create API token button also works, but it carries all the account's access.

Vendor guide: [Atlassian: manage API tokens](https://support.atlassian.com/atlassian-account/docs/manage-api-tokens-for-your-atlassian-account/)

- **Fields:** `url` (the site, such as `https://example.atlassian.net`), `email`, `api-token` and `cloud-id` (optional; read from the site when empty).
- **What uses it:** Create task on cards, ticket links, the `jira-links` pass that moves a card to In progress when a ticket names it (`core.jira-links`), the released issues on deploy reports, and the investigator's `jira_search`, `jira_issue` and `propose_ticket`, with Jira's notes for the investigator. With Jira off, the prompt names none of these tools, and `triage` offers only `handled`. The project, the fields and the other behaviour settings are in Admin > General, under Jira.
- **Release names:** `jira.release-name` (Release name in Admin > General, or `JIRA_RELEASE_NAME`) names the Jira release of a deploy day, and deploy reports look up the issues of these releases. The default is `{project} {yyyy}-{mm}-{dd}`. The placeholders are `{project}`, `{yyyy}`, `{mm}` and `{dd}`, in the local time zone; each may appear at most once, and `{project}`, `{mm}` and `{dd}` are needed. A double quote or a backslash is refused, since the name goes into a Jira search. Admin refuses a value that breaks this, and such a value in the environment or the config file stops the start.
- **Access:** issue reads need Browse projects (scope `read:jira-work`), user reads Browse users and groups (scope `read:jira-user`). Test connection reads `/myself`, then searches the issues updated in the last day and the users. A 401 on `/myself` gives the credentials text, and a 403 names the user permission. Because that sign-in worked, a 401 on a later read means the token lacks a scope (Atlassian answers "401 Unauthorized; scope does not match"), and Test names it. Test also fails on a refused user read: no check reads users, but Create task and the people search do. A refused issue read in the `jira-links` pass raises the warn card `jira-links:access:issues`, and the Jira tile shows No access; any other failure gives the status `error: issues: <reason>`, shown on the tile as an error, and keeps the card. Switching Jira or Jira links off closes the card. A refused release search is named on the deploy report (the issue count reads "not read"), and a refused release lookup during enrichment is named on the deploy. Jira answers 404 both for an issue that does not exist and for one the token cannot see, so `jira_issue` and the Releases page say both, and a deploy with such an issue is read again while it is in the enrichment window. A token without Browse projects may still get an empty search. A refused Create task names the Create issues permission.

### Confluence

Atlassian Cloud only, through the same gateway, with its own email and API token, not Jira's. It is a company knowledge source (`company_knowledge='always'`); its tool is offered only once Spaces is set.

**Set it up:**

1. Sign in as the account Skopax will use and open `https://id.atlassian.com/manage-profile/security/api-tokens`. Choose Create API token with scopes, name it and set an expiry (1 to 365 days).
2. Pick Confluence as the app and select the scopes search:confluence, read:page:confluence and read:space:confluence. The account must be able to view the spaces you list.
3. Create the token, copy it and paste it into `API token`. Enter that account's address in `Email`. Use a token of its own, not the one Jira uses.
4. In `URL` give your Atlassian Cloud address, such as `https://example.atlassian.net`. In `Spaces` list the space keys the tool may read, such as ENG, OPS.
5. Choose Test connection. It names any listed space the account cannot see.

Vendor guide: [Atlassian: manage API tokens](https://support.atlassian.com/atlassian-account/docs/manage-api-tokens-for-your-atlassian-account/)

- **Fields:** `url` (the site), `email`, `api-token` and `spaces`, a comma list of the space keys the tool may read.
- **Tool** `confluence` searches and reads the pages of the allowed spaces, read only, with passwords, keys, tokens and personal data masked. With no space listed it is not offered and reads nothing.
- **Access:** search needs the scope `search:confluence`, pages `read:page:confluence` and spaces `read:space:confluence`. Test connection reads the allowed spaces and fails naming any the account cannot see, then runs a one-page search in them. A 403 names the scope, and so does a 401 after that first read worked or one that says "scope does not match". A page that answers 404 is named as missing or not viewable (the View permission on its space), and an empty search names the allowed spaces the account cannot see.

### SharePoint and OneDrive

One folder of a SharePoint document library or of a OneDrive, read as company knowledge (runbooks, postmortems, design notes; `company_knowledge='always'`) through Microsoft Graph, with an app of your Microsoft Entra tenant. Global Microsoft cloud only. Several instances, one per folder, at most 10.

Preview: the plug-in has not yet been tried on a live tenant; Sites.Selected is expected to cover the item, children and content calls, but Microsoft's pages do not confirm it yet.

**Set it up:**

1. Sign in to the Microsoft Entra admin center as at least an Application Developer. Browse to Entra ID > App registrations > New registration, name the app, keep Single tenant only, and select Register.
2. On the app's Overview page copy the Application (client) ID into `Client ID` and the Directory (tenant) ID into `Tenant ID`.
3. Select Certificates & secrets > Client secrets > New client secret, pick an expiration, select Add, and copy the Value into `Client secret` at once; it is never shown again.
4. Select API permissions > Add a permission > Microsoft Graph > Application permissions, add Sites.Selected only, then a Privileged Role Administrator selects Grant admin consent. Do not add Files.Read.All or Sites.Read.All: they read every site in the tenant.
5. Sites.Selected alone reads nothing until the app is granted one site. That grant needs a SharePoint Administrator who is a member of the site, signed in to Graph Explorer and consented to Sites.FullControl.All under Modify permissions.
6. In Graph Explorer read the site id with GET `https://graph.microsoft.com/v1.0/sites/contoso.sharepoint.com:/sites/ops`, then send POST `https://graph.microsoft.com/v1.0/sites/{site-id}/permissions` with roles ["read"] and grantedToIdentities holding the application id (the Client ID).
7. Fill in `Site URL` and `Folder` (and `Library` when it is not the default one), then choose Test connection.

Vendor guide: [Microsoft Learn: selected permissions](https://learn.microsoft.com/en-us/graph/permissions-selected-overview)

- **Fields:** `tenant-id` (a GUID), `client-id` (a GUID), `client-secret`, `site` (the https address of one site, such as `https://contoso.sharepoint.com/sites/ops`, or of a OneDrive, such as `https://contoso-my.sharepoint.com/personal/jane_contoso_com`; the host must end in `.sharepoint.com`), `library` (optional: the name of the document library; empty is the site's default library) and `folder` (a path inside the library, such as `Runbooks/Prod`). Changing `site` asks for the client secret again.
- **Sign-in:** the app signs in with the client credentials flow at `login.microsoftonline.com` for the scope `https://graph.microsoft.com/.default`, and reuses the token until 5 minutes before it expires.
- **Tool** `sharepoint_docs`, read only, at most 8 calls per run. `op search` lists the documents of the folder and its subfolders whose name or folder path holds the words, at most 20, each with its path, id, link and whether it can be read; it does not search the text inside. `op read` takes a path or an id from a search and returns the text, cut at 20,000 characters: Markdown and text files (`.md`, `.markdown`, `.mdx`, `.txt`, `.rst`, `.adoc`, at most 1 MB) and Word documents (`.docx`, at most 8 MB, paragraph text only; a file with macros or entity declarations is refused). Other files are listed with their link but not read. Passwords, keys, tokens and the other masked values are masked in what it returns.
- **Kept to the folder:** search lists only the folder and the folders under it, at most 60 folders, 40 listing calls, 3,000 documents and 60 seconds (a search that hits a limit, or meets a next-page link off `graph.microsoft.com`, says `partial`), and the listing is reused for 5 minutes. On the way out, an item of another drive, a shortcut to another drive, or one whose path is not under the folder is dropped; a path with an encoded slash or backslash, or an empty, `.` or `..` segment, counts as outside. A read fetches the item's details first (a path is read relative to the folder) and reads it only when they place it under the folder and its chain of parent folders reaches the folder, at most 20 folders up. The file itself is downloaded from the address Microsoft Graph gives, only when it is https on a `.sharepoint.com` host, without the token; a download over the size limit is refused as too large.
- **Why search reads names only:** Microsoft Graph's drive search does not support the Sites.Selected permission, and its Search API needs Files.Read.All or Sites.Read.All, which read the whole tenant. Skopax keeps to Sites.Selected.
- **Links:** while an instance serves, links to its site's host pass the investigator's link check, so it can cite the documents it read. The investigator is told to search the folder for the card's service before it concludes, to cite each document with its link, and to say when nothing was found.
- **Access:** Test connection signs in, finds the site, the library and the folder, and lists the first page of the folder. A refused sign-in names Tenant ID, Client ID and Client secret; a 401 from Microsoft Graph asks to add Sites.Selected and grant admin consent; a 403 names the part and asks to grant the app read on this site (Sites.Selected); a 404 says there is no such site, library or folder, or the app is not granted read on it.

### Argo CD

**Set it up:**

1. With `Mode` set to This cluster no token is needed. Give the service account Skopax runs as get and list on applications.argoproj.io in the namespace in `Namespace` (default argocd), then enter the Argo CD address in `URL` for the links.
2. With `Mode` set to Argo CD API, in the argocd-cm ConfigMap add a local account that can use API keys, such as accounts.skopax: apiKey.
3. In argocd-rbac-cm add the policy p, skopax, applications, get, */*, allow (or a project, such as my-project/*).
4. Create the token with argocd account generate-token --account skopax --expires-in 90d and paste it into `Token`. Enter the Argo CD address in `URL`, then choose Test connection.

Vendor guide: [Argo CD: user management](https://argo-cd.readthedocs.io/en/stable/operator-manual/user-management/)

- **Modes:** `api` reads the Argo CD API with a token; `in-cluster` reads the Application objects of the cluster Skopax runs in, with its service account. Several Argo CD instances can be added.
- **Fields:** `mode`, `url` (the Argo CD server, and the address of the links people open), `token` (api mode), `ca`, `ca-pinned` (as for PMM), `namespace` (default `argocd`: where the Application objects are read in in-cluster mode, and the namespace of the links), `projects` (a comma list; empty means all) and `app-suffix` (a suffix the application names carry after the service name, left out of the service on cards).
- **Check** `argo` every 5 minutes: applications that stay out of sync or unhealthy, in the section `argo`. **Tool** `argo` (`apps`, `app`). Cards link to the application in the Argo CD UI, and the probe calls `<url>/healthz`.
- **RBAC:** in api mode the token's account needs the RBAC permission `applications, get` on the applications to watch. In in-cluster mode the service account needs `get` and `list` on `applications.argoproj.io` in the namespace. A refused list gives the status tail `no access to applications` and one warn card, and keeps the application cards open.
- **An empty list:** Argo CD leaves out the applications a token may not read, so in api mode an empty list is checked with `GET /api/v1/account/can-i/applications/get/*/*`. A yes gives the status tail `no data`. A no counts as a refusal: the warn card, Test connection fails naming `applications, get`, and the tool's `apps` returns an error. A role scoped to one project (`<project>/*`) answers no, so while that project has no applications it shows the warn card and fails Test.
- **One application:** Argo CD answers a missing and a refused application alike, so in api mode an HTTP 403 on one application reads "Argo CD has no application <name>, or the token lacks the RBAC permission applications, get", and a 401 says the token "is invalid or lacks" it.
- **A narrower token:** the list leaves out what the token may not read, so an application whose card is open and that drops out of the list closes its card only when can-i on `*/*` says yes; otherwise the card waits. A token scoped to some projects therefore keeps the cards of the others open until they expire.
- **Large installs:** in-cluster mode reads 50 applications per page, for at most 200 pages or 3 minutes. Past that the status tail is `read <n> of <m> applications` (or `read <n> applications, more not read`), and the applications not read keep their cards. api mode reads the whole list in one call. A pass keeps only the fields it reads of each application, and Test connection asks the list for the names only.

### SonarQube

A card for each SonarQube project whose quality gate starts to fail, from the SonarQube web API with a read-only token. It takes one instance per SonarQube.

**Set it up:**

1. In SonarQube open the account menu > My account > Security. Under Generate Tokens choose the type User, set an expiration and choose Generate.
2. Use an account that has the Browse permission on the projects to watch (Project settings > Permissions of each project). The token reads only what that account may browse.
3. Copy the token at once (SonarQube shows it once) and paste it into `Token`.
4. In `URL` give your SonarQube address. In `Projects` list project keys or names to watch (* matches any characters), or leave it empty for all browsable projects. Choose Test connection.

Vendor guide: [SonarQube: managing tokens](https://docs.sonarsource.com/sonarqube-server/latest/user-guide/managing-tokens/)

- **Fields:** `url` (the SonarQube address, also the address of the links), `token` (a user or project analysis token that can browse the projects; it is sent as the user name of a basic sign-in), `projects` (a comma list of project keys or names, where `*` matches any characters; empty watches every project the token can browse), `ca` and `ca-pinned` (as for PMM). At most 5000 projects are read, and the status says so past that.
- **Check** `sonarqube`, once a day. Its first run records which projects fail now, silently, and opens no card; the first state is kept in the plug-in's memo. After that, a card `sonar-gate|-/<project key>` opens for a project whose gate fails and did not fail at the first run, lists up to 5 failed conditions, and clears when the gate passes. A project that already failed at the first run stays quiet until its gate has passed once; a project first seen later and already failing gets a card. Only a failed gate (`ERROR`) makes a card.
- **How it reads:** the project list (`/api/components/search`), then the public `/api/qualitygates/project_status` of each watched project, one call each, which gives the gate state and its failed conditions. The whole pass has a 200 second budget, Test connection 12 seconds. A pass whose list cannot be read, or in which no gate can be read, fails and changes nothing: no card opens or clears, and the memo is not written. A timeout, a spent budget, a 403 on one project or an answer without a gate leaves those projects unread: the others are judged, only cards of read projects clear, and the status says `<n> not read`. Cards link to the project dashboard, for an `https://` address.
- **Access:** the token needs the Browse permission on the projects it should watch, and reads only those. Test connection validates the token and counts the projects it can browse. A 401 or 403 on the project list opens the access card for the part `projects` and keeps the gate cards open. A SonarQube with no browsable project reads as `no data`: the cards stay and the first state is not recorded yet.

### Slack

Skopax posts a card to the channel, keeps the post up to date, and answers questions asked in its thread.

**Set it up:**

1. At `https://api.slack.com/apps` choose Create New App > From a manifest, pick your workspace and paste examples/slack-app-manifest.yaml from the Skopax repository. It sets the bot scopes, events and Socket Mode.
2. Choose Install to Workspace, then Allow. Under OAuth & Permissions copy the bot token (xoxb-...) and paste it into `Bot token`.
3. Under Basic Information, in the App-level tokens section, generate a token (Socket Mode needs one; if asked for a scope, choose connections:write) and paste the xapp-... token into `App token`.
4. Choose Test connection, then Save & turn on. Restart Skopax once: an app token added or changed later is read only at start.
5. Invite the bot to the channel (/invite @Skopax) and set that channel in Admin > General > Slack > Channel.

Vendor guide: [Slack: app settings](https://docs.slack.dev/app-management/quickstart-app-settings)

- **Creating the Slack app:** at api.slack.com/apps choose "Create New App", then "From a manifest", pick your workspace and paste [`examples/slack-app-manifest.yaml`](../examples/slack-app-manifest.yaml). It turns on Socket Mode, buttons and dialogs, the events the listener reads (mentions, and messages in channels, private channels and direct messages) and every bot scope listed below, plus `im:history`, `reactions:write` and `app_mentions:read`. Install the app, copy the bot token (`xoxb-...`), and create an app-level token with the `connections:write` scope under Basic Information (`xapp-...`).
- **Fields:** `bot-token` (`xoxb-...`) and `app-token` (`xapp-...`, for Socket Mode, which the Slack listener needs). The channel that cards and deploy reports go to is the setting Channel in Admin > General > Slack (`slack.channel`, or `SLACK_CHANNEL`).
- **Test connection** checks the token shapes first, then calls Slack's `auth.test` and fails naming the scopes the bot lacks: `chat:write`, `files:write`, `channels:read` or `groups:read`, `users:read` and `users:read.email`, plus `channels:history` or `groups:history` and `files:read` with an app token. With an app token it then asks `apps.connections.open` for a Socket Mode address, so a revoked app token fails Test. The notices strip names a scope the main channel or direct messages need (see [Notices](setup.md#notices)); the result is stored, so every server copy shows it.
- **Transport:** Test connection goes through the instance's transport; posts, uploads and downloads use core's own Slack client, so a proxy or CA policy set on the transport does not reach them.
- **Socket Mode** is started once, when Skopax starts and the integrations have loaded. An app token added or changed later needs a restart. A new bot token is used by the next call.
- **The investigator posts directly.** When a run finds a card or a deploy report worth telling the channel, it posts at once through the same path as Send to Slack (text, screenshots, thread). There are no drafts, no countdown and no Hold. Slack auto-send (Admin > Features) is the off switch: while it is off nothing posts on its own, the post is dropped with the activity line `Slack post skipped: auto-send is off` and the card reads Not posted: auto-send is off; Send to Slack posts it by hand. A failed post is tried again 3 times, 10 minutes apart; after that the card reads Slack post failed with the reason, and Send to Slack tries again. The Slack tab lists the posts that did not go out.
- With Slack off or not set up, nothing is posted. The investigator's posts wait for Slack for up to an hour; a text older than that is dropped, and the card or report reads Not posted: the text was more than an hour old when Slack was ready.
- **Daily tag report:** on business days Skopax posts one message to the main channel that mentions the responsibles of each tag with open cards (see [Card tags and responsibles](setup.md#card-tags-and-responsibles)). Mentions need the bot scope `users:read.email`, which the manifest above includes; without it the report names people in plain text and Admin shows a warning.
- **Echo of route posts:** with `routes.slack-echo` on (Admin > General > Slack, or `ROUTES_SLACK_ECHO`; default off), a card that a route such as a Teams channel posts is also posted to Slack in the normal way, even when the Slack rules alone would not have posted it, unless it already has a Slack post. The card's Slack thread then gets one line, such as "Sent to seo on Microsoft Teams (tag: seo)". The echo is queued and sent by the route worker, never while a route post or a by-hand send waits, and a card whose post is on its way, being sent or kept for a person, a muted card, and a card while `slack-autosend` is off wait for Slack to post it. See [Echo to Slack](extending.md#echo-to-slack).
- **When a posted card stops firing**, a note in its thread says so once it has stayed quiet for the settle time, with Resolve, and Create task when the card has no ticket. A card whose problem does not fix itself (out of memory, a crash loop, an eviction) reads "Alarm went quiet". A card the investigator tied to a one-off event (see [Event causes](setup.md#event-causes)) reads "Stopped after the event", names the event, and says the cause can come back with the next one; never "Back to normal". Its main post reads "Stopped after the event" instead of "Recovered", and a reminder on it says the cause can come back. The event text is shown as plain text and the note is sent verbatim: mentions and links in it do nothing, and the four Slack mark characters `*`, `_`, `~` and the backtick are shown as look-alike characters.
- **Posts kept for a person.** A run that read planted text does not post: the card reads Not posted: the run read suspicious text, shows the text with Send to Slack, and one INFO line on `skopax.investigator.tools` says so: `Slack post on <key> not sent, kept for a person: the run read planted text in the <tools> answer (<n> flags: <ids>)`. The ids, such as `p3`, are the places of the phrases in `kit.flags.PHRASES`, so the line itself reads as no planted text; a key or tool name that does is logged as `[a name that reads as planted text]`. When a run that read planted text is refused an action (a cause, a business tag, an absorb pattern, a post to the main channel, a Slack message), the Tools page lists it under Held by the planted-text guard, in plain words, and not under Latest errors; a call refused for an argument outside the tool's schema is listed under Refused input, and the top of the page counts errors, held and refused input separately. While Slack auto-send is off, one INFO line on `skopax.autosend` says that Slack posts are skipped, once each time it goes off.
- **Posting at once:** while Slack auto-send is on, a card at or above Post at once from (Admin > General > Slack, `slack.post-sev`, or `SLACK_POST_SEV`; default Warning) is posted to the main channel with its numbers when it is pushed: when it is new, when its severity rises or its value doubles, and when it fires again after it cleared: after 2 hours, or for a taken or resolved card only when its severity rises or its value doubles. A card of a service that was pushed in the last 30 minutes is not pushed again unless its severity is higher. An info card is posted only when it is brand new, and not when its service was pushed or had an info post in the last 30 minutes. A critical card's post says the investigation has started, and a warning's says the investigator looks at it on its next run; with the investigator off, or in a mode other than live, the post has no investigation line. An info card's post says that no automatic investigation runs on it. A muted, grouped, taken or already posted card is not posted this way, and nothing is posted on the first pass after a fresh start. When more than 3 cards are due in one pass, none is posted at once, and the activity log names how many of each severity opened; the investigator, when it runs, groups them first. Set it to Critical to post only critical cards at once, or Off to leave every card to the investigator's posts and to people.
- Admin > General > Slack also holds Push users (`slack.push-user`, the Slack user ids that get phone pushes) and the operator check cache (`slack.operator-cache-seconds`, how long the answer to whether a Slack user may act as an operator is kept).

### Microsoft Teams

Each Teams instance is one route: the Workflows URL of a channel and the cards it takes.

**Set it up:**

1. In Teams open Workflows (the Workflows app, or a channel's More options > Workflows) and create a flow that receives a webhook request and posts to a channel. Pick the team and channel, then Save.
2. Each flow type has its own rule for who may trigger it; pick one that lets the Skopax server post.
3. On the flow's details page copy the HTTP POST URL and paste it into `Webhook`. The URL is a secret: anyone who has it can post to the channel.
4. Fill in `Card tags` or `Card keys`, since a route needs one of them, then choose Test connection. The workflow owner must be allowed to post in that channel.

Vendor guide: [Microsoft Teams: webhooks with Workflows](https://support.microsoft.com/en-us/teams/apps-service/create-incoming-webhooks-with-workflows-for-microsoft-teams)

- **Fields:** `webhook` (the https address Teams Workflows gives for posting to the channel; kept secret), `tags` (card tags, such as `seo, developer`), `keys` (card key patterns with `*` and `?`, such as `log|checkout-*|*`), `mentions` (e-mail addresses mentioned in each post), `min-sev` (the route's own lowest severity; empty uses `teams.min-sev`) and `slack-echo` (core adds this field to every route plug-in: `default`, `on` or `off`; see [Echo to Slack](extending.md#echo-to-slack)). A route needs a known card tag or a key pattern; Admin shows the instance's health as an error without one. Test connection refuses a route with neither filled in, or with an address it cannot post to, before it posts a test card.
- **What it posts:** a route with tags only posts the open cards that carry one of them and are at or above `teams.min-sev`; a route for a business team posts a card once its business sentence is written, by the investigator, an operator or a plug-in (see [Card sentences](extending.md#card-sentences)). A route with key patterns posts the new and escalated cards whose key matches one of them, or that carry one of its tags. Routes that are not for a business team also skip cards below their own `min-sev`; a key route's own value replaces the setting, higher or lower. Information (info findings) is never posted, business routes included; a retired `min-sev` of `info` reads as `warn`. Card texts reach Teams as plain text, never as links. A business post leads with the why, then "Recommended:" and the action; an info post says what is happening. Two tag routes on the same address post a card tagged for both once.
- **Settings** (Admin > General, under Microsoft Teams): `teams.repeat-days` (default 7: days before a card already posted to a channel is posted there again; 0 posts every time), `teams.min-sev` (default `warn`: the lowest severity a tag route posts, and the default of a key route with no `min-sev` of its own) and `teams.fixed-posts` (default off: a fixed message to a business channel when a card it was told about as info closes).
- **Slack echo:** off by default, so an upgrade changes nothing. Switch it on for every Teams route with `routes.slack-echo`, or per route with its `slack-echo` field (`on` or `off`; empty or `default` follows the setting). A card posted to a route that echoes also goes to Slack when Slack has no post for it, and Slack's thread says "Sent to <route> on Microsoft Teams (tag: <tag>)". A line waits up to 24 hours for Slack to post a card it may not post yet (see Slack above). A failed Slack post never blocks or retries the Teams post, and a failed Teams post says nothing on Slack.
- **Cards left out:** a card whose check sets `noRoutes` is never posted automatically; it can still be sent by hand.
- **By hand:** an operator posts a card through a route from the Notify row on the board or Send to... on the card page.
- **Delivery:** route posts run in a background worker, never inside a pass. An automatic post that fails, or gets no answer within 10 seconds, is tried again at the next delivery, then after 2, 4, 8 and 16 minutes: at most 6 tries per card and route. A route that is still busy with an earlier post costs no try. While its last post failed, the instance shows `error: <route>: <reason>` in Connect > Integrations. After the last try the notices strip names the route; a Test connection that passes, or a new webhook, lets those cards be posted again. A slow post that succeeded late can arrive twice. A post by hand that fails says so at once, with the chat tool's answer, and a card sent by hand is not posted again by the retry.
- **Access:** a 403 on the test post names what the Workflows URL needs: a workflow that is switched on, that anyone may trigger, and whose owner may post in that channel. A 404 says no workflow is at that address. A 401 gives the credentials text. Routes have no primary instance: every route posts.

## Web, app and marketing plug-ins

Skopax also ships Website, Umami, Chrome UX Report, Safe Browsing, App stores, Crashlytics, Google Analytics 4, Google Tag Manager, Google Ads and VWO. As with the observability plug-ins, each does nothing until an admin adds an instance, a check runs every 15 minutes unless its entry says otherwise, its id is also its pass name, and a refused read follows [When a credential lacks a permission](#when-a-credential-lacks-a-permission). Their check ids and ran keys are reserved instance names in the same way (`browser`, `pagescan`, `pagescan-digest`, `umami`, `crux`, `safebrowsing`, `appstore`, `play`, `play-reviews`, `crashlytics`, `ga`, `gtm`, `gads`, `gads-policy`, `wingify` and `wingify-verdict`), and a VWO instance also cannot be named `verdict`.

- **Website:** the `page` tool reads a page of your site as a visitor without cookies sees it, the `screenshot` tool shows it to the investigator as a picture, and `page_scan` reads one page as the page scan does. They open only the host of `url`, its subdomains and `hosts`, never a login, API or click-counting path. A subdomain that is not the `url` host or a `hosts` entry is opened only when it resolves to public addresses, and a page read connects to the address that was checked; switch on `internal-subdomains` ("Subdomains may be internal") when such subdomains live on your internal network. Page reads honour `HTTPS_PROXY`, `HTTP_PROXY` and `NO_PROXY`: through a proxy they open a CONNECT tunnel to the checked address, so the pin holds, and a proxy that refuses the tunnel is named in the error. The proxy must be an `http://` URL (an `https://` proxy URL is refused), and `NO_PROXY` is matched against the host name, not the checked address. In the browser the check is best-effort: Chrome looks the name up again, so Skopax also refuses a shot whose page or frame on such a subdomain answered from a private address. That address check assumes no proxy sits in front of the browser service; behind one, Chrome reports the proxy's address. A bare public suffix such as `co.uk` is never a site host, and Test connection names it. Check `browser` (every 5 minutes) watches the browser service. Checks `pagescan` and `pagescan-digest` are the page scan. The check, the tools and the page scan open only https sites on port 443, so the demo stack, which serves plain http on its own port, cannot serve the plug-in a page: point it at a real https site.

  **Set it up:**

  1. No account is needed. Set Site URL to the https address of the site, for example `https://www.example.com`. Only https on port 443 is read.
  2. List other hosts of the site in More hosts, and hosts behind a login or APIs in Login hosts, Login paths and API paths: they are never opened.
  3. For screenshots, run a headless Chrome service that exposes the DevTools endpoint and set Browser URL to it, for example `http://browser:9222`. Empty means no screenshots.
  4. If a firewall or bot rule blocks the page read, add the header it expects (Name: value) in Scan headers, and ask the site owner to allow the Skopax address.
  5. Optionally link your Umami instance in Umami instance so the page scan reads the busiest paths. Test connection reads the page, then the browser.

  Vendor guide: [chromedp headless-shell image](https://github.com/chromedp/docker-headless-shell)
  - **Fields:** `url` (required: the site, https), `hosts` (more hosts of the site; their subdomains count too), `login-hosts`, `login-paths`, `api-paths` and `refused-paths` (comma lists of hosts and path segments or paths that are never opened; `/api/` is always refused), `browser-url`, `description` (one or two sentences on the site, for the page scan), `umami` (the Umami instance whose busiest paths the page scan reads), `scan-headers` and `internal-subdomains` (default off). Settings (Admin > General, under Website): `website.page-scan`, `website.scan-hours`, `website.recheck-days`, `website.pages`, `website.extra`, `website.ignore` (words never reported as typos), `website.model`, `website.digest-hour` and `website.scan-prompt` (see [The page scan](#the-page-scan)).
  - **Options**, each empty by default, and an empty option changes nothing: `tracking-params` (comma-separated query parameter names that count as ad tracking on top of `utm_*`, `gclid`, `fbclid` and `msclkid`; a trailing `*` takes every parameter with that start, such as `pk_*`; the tools refuse such a url, exactly as they refuse `utm_*`, and never strip the parameter and fetch), `kept-headers` (comma-separated response header names the `page` tool returns on top of its usual ones; cookie and authorization headers, and any name that contains token, secret, session, auth, cookie, password or credential, are never returned), `scan-skip` (comma-separated path prefixes the page scan never reads, on top of its built-in ones, and the paths below them; case is ignored and a missing leading slash is added) and `user-agent` (printable ASCII, at most 200 characters: sent by the `page` tool and Test connection instead of the built-in one, and used as the closing product token of the screenshot browser's identity; Test connection refuses one a header cannot carry).
  - **Access:** no credential. Test connection reads the `url` page, with `scan-headers`, then the browser's version; a site that answers 401 or 403, or a browser that refuses, fails it. Check `browser` opens the access card `browser:access:browser` when the browser refuses the version read. The page scan opens `pagescan:access:site` when the site answers 401 or 403 to the pages it scans (fix it with `scan-headers`), and `pagescan:access:umami` when the linked Umami instance refuses its busiest paths (the Umami credential needs view access to the website).
- **Umami:** pageviews and one tracked event of a website, hour by hour against 1 and 7 days earlier (check and tool `umami`). Umami Cloud takes an API key; your own server takes a token, or the username and password of a user that can view the website, and its `ca` and `ca-pinned` work as for PMM. A drop card names the latest Google Tag Manager publish of the 48 hours before it.

  **Set it up:**

  1. Umami Cloud: sign in, click the profile button in the side navigation, choose Settings, API keys, Create key, and copy the key.
  2. Umami Cloud: set URL to `https://api.umami.is/v1` and paste the key into API key. Leave Token, Username and Password empty. Set UI URL to your dashboard address, or cards carry no link.
  3. Own server: set URL to the server address. Token takes a bearer token: an API key (Settings, API keys, if your version has them) or a login token. Without Token, Username and Password log in and renew themselves.
  4. Whichever you use, the key or user must own the website, be an admin, or be in the team that holds it. Use a user with view access only.
  5. Website ID: copy the UUID from the website settings page, in the tracking code (data-website-id), and paste it into Website ID.
  6. Leave Event empty to watch pageviews only, or name a tracked event. Test connection reads the website.

  Vendor guide: [Umami: API key](https://docs.umami.is/docs/cloud/api-key)
  - **Fields:** `url` (required: `https://api.umami.is/v1` for Umami Cloud, or the address of your server), `ui-url` (the address people open for links; Umami Cloud needs it for cards to carry a link), `api-key` (Umami Cloud), or `token`, or `username` and `password` (your own server), `website-id` (required: the UUID of the website), `site` (the service name on cards; empty uses the domain Umami reports), `event` (one tracked event; empty watches pageviews only), `properties` (default `type`: event-data properties; the first one splits the event into one count per value), `ca` and `ca-pinned`.
  - **Access:** the API key, token or user must be able to view the website. Test connection signs in and reads the website. A refusal opens the access card for the part `website`.
- **Chrome UX Report:** INP, LCP and CLS that real Chrome users had on your origins, once a day against a week earlier (check and tool `crux`), with a Google API key.
  - **Set it up:**

    1. In Google Cloud console, open APIs & Services > Library, search Chrome UX Report API and click Enable.
    2. Open APIs & Services > Credentials > Create credentials > API key. Under API restrictions choose Restrict key, select only Chrome UX Report API, and save.
    3. Paste the key into API key.

    Vendor guide: [Chrome UX Report API](https://developer.chrome.com/docs/crux/api)
  - **Fields:** `api-key` (required) and `origins` (required: a comma list of origins; a bare host name means https, and a path is ignored).
  - **Access:** the key needs the Chrome UX Report API enabled. A 403 names that and opens the access card for the part `origins`.
- **Safe Browsing:** whether Google lists your hosts as unsafe (check and tool `safebrowsing`). With an API key it asks the Safe Browsing API; without one it reads the public site status page, and the tool is offered only from an instance that has a key.
  - **Set it up:**

    1. Optional: without a key, Skopax reads the public site status page. For the API, open APIs & Services > Library in Google Cloud console, search Safe Browsing API and click Enable.
    2. Open APIs & Services > Credentials > Create credentials > API key. Under API restrictions choose Restrict key, select only Safe Browsing API, and save.
    3. Paste the key into API key.

    Vendor guide: [Safe Browsing API: get started](https://developers.google.com/safe-browsing/v4/get-started)
  - **Fields:** `api-key` (optional) and `hosts` (required: a comma list of host names).
  - **Access:** with a key, the key needs the Safe Browsing API enabled; a 403 names that and opens the access card for the part `hosts`. Without a key the status page is read one host every few seconds.
- **App stores:** App Store ratings (check `appstore`) and Google Play vitals and reviews (check `play`), tool `stores`. An empty app id or package skips that store, and Test connection also skips a store whose check is switched off.
  - **Set it up:**

    1. App Store ratings need no credential: enter the digits after id in the App Store link in App Store app id. The steps below are for Google Play only.
    2. In Google Cloud console, open APIs & Services > Library, enable Google Play Developer Reporting API and Google Play Android Developer API.
    3. In Google Cloud console, open IAM & Admin > Service accounts, pick your project and click Create service account. Name it and click Done; no project role is needed.
    4. Open the account, Keys tab, Add key, Create new key, JSON, Create. Paste the whole downloaded file into Service account key.
    5. Copy the account email (`client_email` in the key). In Play Console, Users and permissions, Invite new users, enter that email, grant App permissions on the app with View app information (read-only), then Invite user. If Play is still refused, an owner may need to grant more.
    6. Enter the Android package name, such as com.example.app, in Google Play package. Empty skips Google Play, so no Play data is read.

    Vendor guide: [Play Console: invite users and set permissions](https://support.google.com/googleplay/android-developer/answer/9844686)
  - **Fields:** `apple-app-id` (the digits after `id` in the App Store link), `countries` (default `us`: at most 10 two-letter countries whose ratings are read), `play-package` (the Android package name, such as `com.example.app`) and `credentials` (needed for Google Play only).
  - **Access:** App Store ratings need no credential. Google Play needs the service account with View app information (read-only) in Play Console; a refused part (Play vitals or Play reviews) opens its own access card, and the other part is still read.
- **Crashlytics:** crash and freeze spikes and new crashes of the mobile apps (check `crashlytics`, tool `crashes`).
  - **Set it up:**

    1. In Google Cloud console, select your Firebase project, open APIs & Services > Library, search Firebase Crashlytics API and click Enable.
    2. In Google Cloud console, open IAM & Admin > Service accounts, pick your project and click Create service account. Name it and click Done; no project role is needed.
    3. Open the account, Keys tab, Add key, Create new key, JSON, Create. Paste the whole downloaded file into Service account key.
    4. Copy the account email (`client_email` in the key). Open IAM & Admin > IAM on the same project, click Grant access, enter that email and pick the role Firebase Crashlytics Viewer.
    5. Enter the Firebase app ids of your apps in Android app id and iOS app id. An empty one skips that app.

    Vendor guide: [Firebase: predefined IAM roles](https://firebase.google.com/docs/projects/iam/roles-predefined-product)
  - **Fields:** `project` (the Firebase project id, shown in the Test result), `android-app-id` and `ios-app-id` (Firebase app ids, such as `1:<project number>:android:<hex>`; an empty one skips that app) and `credentials` (required).
  - **Access:** the Firebase Crashlytics Viewer role (`roles/firebasecrashlytics.viewer`). A refused app opens an access card for that app, and the other app is still read.
- **Google Analytics 4:** daily drops in key events and active users (check `ga`, once a day; tool `analytics`). A web drop card names the latest GTM publish from two days before its day to a day after it, which covers a property whose time zone runs behind the server's.
  - **Set it up:**

    1. In Google Cloud console, open APIs & Services > Library for your project, search Google Analytics Data API and click Enable.
    2. In Google Cloud console, open IAM & Admin > Service accounts, pick your project and click Create service account. Name it and click Done; no project role is needed.
    3. Open the account, Keys tab, Add key, Create new key, JSON, Create. Paste the whole downloaded file into Service account key.
    4. Copy the account email (`client_email` in the key). In Google Analytics, Admin > Property access management, click +, Add users, enter that email, choose Viewer, click Add.
    5. Put the numeric property id (Admin > Property settings) in Web property id. Use App property id only for a separate app property.

    Vendor guide: [Google Analytics: add users to a property](https://support.google.com/analytics/answer/9305788)
  - **Fields:** `property-id` (required: the numeric id of the web property), `app-property-id` (a separate property of the mobile apps, read by the tool only; empty when the apps report into the web property), `site` (the service name on cards; empty shows the property id) and `credentials` (required).
  - **Access:** the Viewer role on the GA4 property. A refusal opens the access card for the part `web`.
- **Google Tag Manager:** a new live container version opens an info card (check `gtm`, every 5 minutes; tool `gtm`), and each publish is listed beside the deploys under Releases.
  - **Set it up:**

    1. In Google Cloud console, open APIs & Services > Library for your project, search Tag Manager API and click Enable.
    2. In Google Cloud console, open IAM & Admin > Service accounts, pick your project and click Create service account. Name it and click Done; no project role is needed.
    3. Open the account, Keys tab, Add key, Create new key, JSON, Create. Paste the whole downloaded file into Service account key.
    4. Copy the account email (`client_email` in the key). In Tag Manager, Admin, Container column, User Management, click +, Add users, enter that email and give it Read on the container. Send the invitation.
    5. Enter the public id such as GTM-ABC1234 in Container id.

    Vendor guide: [Tag Manager: add users and permissions](https://support.google.com/tagmanager/answer/6107011)
  - **Fields:** `container` (required: the public id, such as `GTM-ABC1234`) and `credentials` (required).
  - **Access:** Read on the container. A refusal opens the access card for the part `container`.
- **Google Ads:** check `gads` opens the final URL of every enabled ad and opens a card when the page is broken (404, 410 or 5xx) or redirects elsewhere. Without a Website instance in its `website` field it opens only public addresses, and connects to the address it checked: link a Website instance when your own site resolves to internal addresses inside your network. With a Website instance, only that site's hosts are opened, so ads that point at other domains, such as a campaign site, are not checked. Check `gads-policy` raises one warning card per campaign with disapproved ads, naming the ads and their policy topics; the card clears when the ads are approved again, paused or removed. Tool `google_ads`. Disapproved-ads cards (`ad-disapproved`) close as auto-resolved 24 hours (24 business hours while Business days only is on) after they first appeared, firing or not, unless someone acted on them; Admin > General, under Google Ads, lists `google-ads.auto-resolve-hours` (0 to 720, default 24; 0 uses the general [auto-resolve rule](configuration.md#settings)).
  - **Set it up:**

    1. In Google Cloud console, open APIs & Services > Library for your project, search Google Ads API and click Enable.
    2. In Google Cloud console, open IAM & Admin > Service accounts, pick your project and click Create service account. Name it and click Done; no project role is needed.
    3. Open the account, Keys tab, Add key, Create new key, JSON, Create. Paste the whole downloaded file into Service account key.
    4. Copy the account email (`client_email` in the key). In Google Ads, Admin > Access and security, add that email with the access level Read only, and send the invitation.
    5. No developer token is needed since Google ended them on 9 Sep 2026; leave Developer token empty. Production accounts need Explorer access: Google Cloud console, Google Ads API Overview, apply for the Cloud project that owns the service account.
    6. Enter the 10-digit id in Customer id. If the account is reached through a manager account, put that account's id in Manager account id.

    Vendor guide: [Google Ads API: access levels](https://developers.google.com/google-ads/api/docs/api-policy/developer-token)
  - **Fields:** `customer-id` (required: the 10-digit account id), `login-customer-id` (only when the service account reaches the account through a manager account: that account's id), `developer-token` (optional: Google ended developer tokens on 9 September 2026 and ignores them; leave it empty, or enter an existing token of 10 to 100 letters, digits, `-` or `_`, which is then sent; production accounts need Explorer access for the Cloud project), `credentials` (required), `api-version` (default `v26`; raise it when Google retires a version) and `website` (the Website instance whose hosts the ad-link check follows).
  - **Access:** the service account as a Read only user of the account, and a Cloud project with Google Ads API access (Explorer for production accounts). A refusal opens the access card for the part `account`.
- **VWO:** A/B tests that start, stop or change (check `wingify`, every 5 minutes) and the results VWO decided on (check `wingify-verdict`, hourly), tool `wingify`. An API token with Browse permission is enough. A/B result cards (`ab-verdict`) close as auto-resolved 24 hours (24 business hours while Business days only is on) after they first appeared, firing or not, unless someone acted on them; Admin > General, under VWO, lists `vwo.auto-resolve-hours` (0 to 720, default 24; 0 uses the general [auto-resolve rule](configuration.md#settings)).

  **Set it up:**

  1. Sign in to VWO as an admin of the account (API access depends on the plan) and open `https://app.wingify.com/#/developers/tokens`.
  2. Create a token and choose the lowest permission level: Browse is enough, because Skopax never changes anything.
  3. Copy the token and paste it into API token.
  4. Leave Account id as current to use the account the token belongs to, or enter the account number. Test connection reads the account, then a campaign.

  Vendor guide: [VWO: how to access the API](https://help.wingify.com/hc/en-us/articles/58822048638489-How-to-Access-Wingify-API)
  - **Fields:** `api-token` (required) and `account-id` (default `current`: the account the token belongs to).
  - **Access:** Browse permission; nothing is ever changed. A refused part (the account, campaigns or campaign results) opens its own access card.

### Google service accounts

App stores (for Google Play), Crashlytics, Google Analytics 4, Google Tag Manager and Google Ads each have their own `credentials` field: the JSON key of a Google service account, at most 4000 characters. You may paste the same key into each or use one account per plug-in; no plug-in reads another's key. Give the account read access in each product: View app information in Play Console, the Firebase Crashlytics Viewer role, the Viewer role on the GA4 property, Read on the Tag Manager container, and a Read only user in Google Ads. Google Ads needs no developer token (see Google Ads above: Google ended developer tokens on 9 September 2026 and ignores them). A malformed or refused key fails Test connection with a fixed sentence, and the key never appears in a response, a log line or an error.

### The browser service

The `screenshot` tool and the page scan need a headless Chrome reached through its DevTools endpoint, set as `browser-url` on the Website instance; the `screenshot` and `page_scan` tools are offered only from an instance that has one. Run it as its own service, so the browser never holds Skopax's secrets. With Docker Compose, a service like this one works; it is not part of `docker-compose.yml`:

    browser:
      image: chromedp/headless-shell:157.0.8103.15
      restart: unless-stopped
      shm_size: 1gb

Set `browser-url` to `http://browser:9222` and publish no port. Skopax looks the name up and connects to the address, because Chrome refuses a DevTools request addressed by name. Skopax always reaches the browser service directly: `HTTP_PROXY`, `HTTPS_PROXY` and `NO_PROXY` apply to page and API reads only, never to the DevTools endpoint or its socket. The image carries few fonts, so text in a script it lacks, such as Chinese or Japanese, shows as empty boxes; add the fonts your site needs to the image.

Each shot uses a fresh browser profile without cookies, loads nothing from other hosts or API paths, opens no WebSocket and sends no tracking ping. Shots are at least 5 seconds apart, at most 3 per investigator run and at most `screenshot.max-per-day` per day (60 by default) across all runs. Web workers, shared workers and service workers are switched off, and a JavaScript dialog is dismissed and named in the shot's facts. Check `browser` reports "ok, browser down" while the browser does not answer; Admin and the Website tile show it as a warning, "browser down". After 5 minutes without an answer it opens a `browser-down` card, and it opens an access card when the browser refuses the request, for example behind a proxy that asks for credentials. When `slack.attach-screenshots` is on, the investigator's Slack posts carry the shots it took in their thread; switch it off to keep shots out of Slack.

### The page scan

The page scan is off by default; switch it on with `website.page-scan`. It needs `browser-url`. Without the Anthropic key, or with the investigator off or at its daily cap, only the code layout checks run.

- Every `website.scan-hours` (1 by default) it loads up to `website.pages` pages (50) at phone width: the `website.extra` pages first, then the busiest paths of the Umami instance named in the Website instance's `umami` field.
- Code checks the layout. The model (`website.model`) reads new or changed text for typos and the pictures for layout problems, guided by the instance's `description`; `website.scan-prompt` replaces the built-in instructions. An unchanged page is read again after `website.recheck-days` (7).
- Cards are `page-typos|<page>` and `page-layout|<page>`. A box that scrolls sideways inside the page is fine: it raises no card and takes no picture, and the scan only notes it as "reachable by swiping". It becomes a finding when the box is cut off, the whole page is wider than the screen, or the model reports something in it that cannot be reached on a phone. Content clipped only by the page itself (`overflow-x: hidden` on `body` or `html`) can still be reached by panning a phone sideways, so it counts as a page wider than the screen, not as cut off. A clipped box counts as cut off only when readable content (text, links or controls) lies more than 4 px past its clipping edge; decoration is not reported: aria-hidden parts, images and SVG, pseudo-elements, transformed or animated parts, text cut with an ellipsis and visually hidden screen-reader text. Boxes cut off the same way are one finding that counts them and marks each.
- The page text, the column names and the pictures go to the model as data, never as instructions, behind a fixed preamble that `website.scan-prompt` cannot remove. A page line that reads like instructions to an AI is held back from the model, and the row and the pass status count the held lines; the rest of the page is still reviewed as text, but its pictures are not sent, because they would show the held line, until the page text changes. Core also scans every model call as a whole and holds it when page text outside the lines, such as a column name, reads that way. A typo's fix must stay close to the wrong text, and links are cut out of what the model writes.
- Every model call counts against the daily AI spend cap and shows on the Costs page as `pagescan`. Once the cap is reached, the scan stops calling the model until the next day.
- Once a day at `website.digest-hour` (10, local time; -1 for none) one digest goes to the main Slack channel, with the pictures in its thread. A digest line is cut at a word edge; a single word longer than the line, such as a long link, is cut at the limit. A reply carries at most 3 pictures and a thread at most 8; pictures over those caps are left out, and one log warning per pass names them.
- `scan-headers` adds headers to the scan's, the screenshots' and Test connection's requests, for example one your firewall lets through. Separate headers with semicolons; they go only to the `url` host and the `hosts` entries, and Test connection names the headers it parsed.
- `scan-skip` on the Website instance leaves more path prefixes out of the scan, next to `/_next`, `/api`, `/cdn-cgi`, `/static`, `/images`, `/assets` and `/fonts`.
- The layout check that finds boxes cut off at the screen edge counts only real findings against its cap, so a page with many harmless clipped boxes cannot hide a real cut; when its 3 second budget runs out, the shot's facts say `overflowCutShort`. A transform that changes nothing, such as `translateZ(0)` or `scale(1)`, no longer excuses the text under it, unless the element is a carousel track (two or more slides, each narrower than the screen, laid out side by side wider than it). A track, at rest or mid-slide, and a transform that moves or scales something still excuse the text under them; a slide on screen whose own text is clipped is still reported.

## Outside MCP servers

The MCP server plug-in, which ships with Skopax, lets investigator runs and chat answers use the tools of an outside MCP server, such as Atlassian, Notion or GitHub. It is the other direction from the MCP server switch and Connect MCP, which let AI assistants call Skopax.

**Set it up:**

1. Get the streamable HTTP address of the MCP server from its vendor or your platform team and paste it into URL. It must be https.
2. Token in a header: create an access token on the server with read permissions only. Set Method to Token in a header, keep Header name as Authorization, and write Bearer, a space and the token in Header value.
3. OAuth: set Method to OAuth and save, then choose Connect in the connection panel and sign in as an account that can read what the investigator should see. Skopax finds the sign-in details the server publishes.
4. OAuth scopes: list scopes separated by spaces, or leave it empty. If the server does not register clients itself, create an OAuth client in its admin console, register `<SKOPAX_BASE_URL>/api/admin/mcp-tools/oauth/callback` as the redirect address, and enter OAuth client id and OAuth client secret.
5. Test connection lists the tools. Then turn on only the read tools you want in the panel; a new connection starts off.

Vendor guide: [MCP: authorization](https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization)

A connection's panel in Connect > Integrations, with its access, the tools it can look up and its knowledge source:

<picture>
<source media="(prefers-color-scheme: dark)" srcset="images/integrations-dark.png">
<source media="(prefers-color-scheme: light)" srcset="images/integrations-light.png">
<img width="560" src="images/integrations-light.png" alt="Connect, Integrations: five connected tiles, and the panel of an MCP server connection listing its read tools switched on, a write tool switched off and the repository it searches">
</picture>

- **Adding one.** In Connect > Integrations, "+ Add custom MCP" (next to "+ Add integration", for admins, while the MCP server plug-in works) opens the blank new-connection form with the URL and the sign-in, the same as picking the blank MCP server entry below. "+ Add integration" has a group "MCP servers": a blank MCP server, the catalog entries Atlassian (Jira and Confluence), Notion and GitHub, and any entry marked coming soon. Google Drive, whose entry is a ready knowledge source, carries the Knowledge badge. A catalog entry fills in the URL and the sign-in, and shows the form of the token as a hint, never a value. Google Drive is marked Preview, since Google still calls its server a preview, and needs a client id and secret from your own Google Cloud console. Each connection is an instance of the plug-in; open it from the MCP server tile to reach its panel. A new connection is always saved off, and it can be turned on only once its warnings are confirmed (see [Who can read what](#who-can-read-what)). At most 20 connections.
- **Sign-in.** Three modes: none; a header with a token (Header name, `Authorization` by default, and Header value, for example `Bearer` and the token), sealed like every instance secret; or OAuth. For OAuth, choose Connect in the connection's panel. Skopax reads the sign-in details the server publishes (its OAuth protected resource metadata, without which there is nowhere to sign in), requires PKCE with S256, and registers itself where the sign-in server allows it. Where it does not, enter a client id and secret from the server's admin console and register `<SKOPAX_BASE_URL>/api/admin/mcp-tools/oauth/callback` there as the redirect address. OAuth needs `SKOPAX_BASE_URL` to be an https address (or `http://localhost` or `http://127.0.0.1` with a port), and the admin who started a sign-in must finish it within 10 minutes. The sign-in is renewed 5 minutes before it expires. When a renewal is refused, or the sign-in expires with nothing to renew it, the sign-in ends: the connection leaves runs, the notices strip says it needs a new sign-in, and Reconnect starts one. A renewal that fails with a server error, a timeout or a rate limit is tried again after 1 minute, or after the server's `Retry-After` (at most 1 hour), and the current sign-in is used while it lasts. When several Skopax pods share the database, one of them renews and the others wait for it, so a renewal is never sent twice.
- **URL rules.** The URL is the server's streamable HTTP endpoint. It must be https, with no user name, password or `#` part; plain http works only for `localhost`, `127.0.0.1` and `::1` while the MCP server setting Allow plain http to this machine is on, for local testing. Skopax never calls a link-local or cloud metadata address, never connects when the server's name resolves to one (or to a loopback address, unless the name is `localhost`), and follows no redirects. Every address the server names for its sign-in must be https and passes the same checks. Compressed answers and answers over 2 MB are refused. A server with a private CA works with the CA certificate field. There is no `ca-pinned` switch here: MCP servers are reached by an async HTTP client, and a pinned certificate is used only by the core transport. MCP servers are reached directly: `HTTPS_PROXY` is not used, and proxy support comes in a later version. Changing the URL starts the connection over: its tool list, folder, confirmation and sign-in are cleared, and the header value or client secret it uses must be entered again in the same change. A new or cleared `ca` asks for that secret again too.
- **Test connection** lists the server's tools within 12 seconds and reports the server's name and the number of tools. A server that lists no tools fails, since the account may lack a permission. A refusal names the credential (the token in its header, or the sign-in of the connected account) and the scope the server asks for in its `WWW-Authenticate` answer, or else "the needed permission"; for a connection without a sign-in it only says the server refused; a 401 that reports no missing scope says the credentials were refused. An OAuth connection is tested after Connect.
- **What it can look up.** The connection's panel lists the tools that Refresh tools found, at most 500, each with a kind taken from the server's hints:
  - Reads (`readOnlyHint`): the first listing switches these on, up to 40.
  - May change data (no hint): turned on only after you tick I understand.
  - Changes data (`destructiveHint`, or `readOnlyHint` false): locked, because tools that write come in a later version. A tool whose name starts with `execute` and contains `write` or `destructive`, in any case, counts as Changes data whatever its hints say, on every connection. The Atlassian entry, and any connection to its server address with or without the entry, also keeps `executeWrite` and `executeDestructive` locked.

  A tool is also locked when its name, a field name or its input description (over 8,000 characters once cleaned) cannot be sent to the AI provider, and when its name holds a secret of the connection, which is then shown masked. Descriptions are cut at 1,000 characters, freed of control characters and prefixed with the connection's name. A tool whose title, description or input reads like an instruction to the AI starts off and shows the phrase, from the list the investigator also scans tool answers for, such as "ignore previous", "system prompt", "you are a" or "assistant". Some real tools trip it: read the description, and turn the tool on if it is harmless. At most 40 tools of one connection are on.
- **Refreshes.** Refresh tools lists the server again. A tool whose description, input or hints changed is switched off and marked Changed on the server, new tools come in off, and the others keep their switches.
- **The run-start check.** Each investigator run lists every connection again (10 seconds; a good answer is reused for 60 seconds) and compares each switched-on tool with the stored listing. A tool that changed or is gone is switched off, with an activity line and a notice; choose Refresh tools before turning it on again. A connection whose listing fails is left out of that run, with an activity line. When the server refused it, or refused a tool call, the notices strip says so until a listing works again. A switched-off connection raises no notices.
- **In a run.** A tool reaches the model as `mcp_<connection>_<tool>`; a long name is cut and ends in a short hash. Its answer is cut at 20,000 characters, masked like every tool answer, framed as outside data, not instructions, and scanned for planted phrases. A server's error answer is masked and framed the same way, cut to fit 300 characters. A call refused for permission is a tool error, never an empty answer, and names the missing scope when the server gives one; for a connection without a sign-in it only says the server refused. Each call writes one activity line with its time, outcome and the characters read, never its arguments; the run's record of tool calls keeps the masked arguments, cut at 2,000 characters. The tools are offered only while the AI investigator is on, and in the chat only while Chat is on. Shadow mode does not hold calls to outside MCP servers: only read tools, and tools you turned on with I understand, are ever offered.

### Who can read what

- **One account per connection.** A connection acts as the one account it is connected with: the header token or the OAuth sign-in. Everyone who may use the connection reads what that account can read, so connect a narrow service account made for Skopax, not a personal or admin login. Sign-ins per person come in a later version.
- **Chat and asks.** "Who may use it in chat and asks" (default `operator`) is the lowest role whose questions may use the connection: questions on the page, on the Chat page, over the API, in Slack and its direct messages, and over MCP. A question counts with its asker's Skopax role; one asked over MCP counts with the role of that MCP call. A Slack user who is not an active Skopax user, and an API token, count as `viewer`.
- **Card runs** always may use it: card investigations, Investigate clicks and deploy-report assessments.
- **The confirmation.** An admin reads the connection's warnings in its panel and confirms them: who can read what it reads, which account to connect, that document text goes to Anthropic, that answers are kept in chat history and card notes, that a document can try to steer the AI, that personal data can appear in answers, and that each lookup costs tokens. Confirming needs a tool list; an OAuth connection must be connected, and a knowledge source needs its folder. Who confirmed, and when, shows in the panel and in the activity log. The confirmation lapses, and the connection leaves runs until an admin confirms again, when its URL changes, wherever it is set (Admin, the config file or a settings source; list its tools again before you confirm), when its sign-in fields, header value, CA certificate, catalog entry or Knowledge source switch change in Admin, at each OAuth sign-in and disconnect, when a folder is picked, when Read without login is switched (the warnings then also name everyone who opens the board), and when the warnings change. A connection that is on without a current confirmation shows Needs confirmation as a warning. One that needs a new sign-in, or whose last listing or tool call the server refused, shows as an error with the text of its notice. Connections from the config file or a settings source also reach runs only once confirmed in Admin.

### Company knowledge

A connection is a knowledge source when its catalog entry has a ready knowledge adapter and its Knowledge source field is on. Knowledge source cannot be switched on for any other connection. In this version only Google Drive has one. Connect > Integrations marks these tiles with the Knowledge badge, as it does the plug-ins that mark themselves a knowledge source (`company_knowledge`, see [Extending Skopax](extending.md)), and the Knowledge chip lists them.

- **One folder.** Pick it in the panel: search, then choose from the folders the server lists; a pick must come from a list read in the last 15 minutes. Only the files directly in the folder are read, not its sub-folders. A knowledge source may have only its adapter's tools on (for Drive, `search_files`, `get_file_metadata` and `read_file_content`); its other tools are locked. The Google Drive and GitHub entries keep their own server address; to use another URL, clear the catalog entry.
- **Enforced on both sides.** On the way in, Skopax adds the folder to every search and refuses a search that names a folder itself or could break out of the one added. It reads a file only after the file's own details place it directly in the folder, which costs one more call; folders, shortcuts and comments are not read. On the way out, every result outside the folder, or whose place cannot be checked, is dropped; when none is left, the model is told that nothing could be confirmed inside the folder.
- **Citations.** The investigator is told to search the folder for the card's service (its runbook, past postmortems and design docs) before it concludes, to cite each document it used with its link, and to say when a search found nothing. While a Drive source is offered in a run, links to `docs.google.com` and `drive.google.com` pass the investigator's link check. The folder's name reaches the prompt only in quotes and cut at 80 characters. This part of the prompt stays even when an org pack leaves out the plug-in sections; with no knowledge source offered, the prompt is unchanged.
- **Coming soon.** For documents in GitHub, the native GitHub plug-in's `docs` field is ready now (see [GitHub](#github)). GitHub's knowledge adapter reads GitHub's search results as GitHub documents them, but how the hosted server names the place of a file has not been checked on a real account yet, so GitHub is tools only for now, and its Knowledge source shows Coming soon. The same goes for Confluence (through the Atlassian entry) and Notion, whose search results are not documented to say where each item comes from. For SharePoint and OneDrive, use the native plug-in (see [SharePoint and OneDrive](#sharepoint-and-onedrive)). Any other server is tools only.

### Limits

Per investigator run:

| Limit | Value |
|---|---|
| Tools offered from MCP servers | 60, with at most 60,000 characters of their definitions |
| Calls of one tool | 8 |
| Calls to MCP servers | 20 |
| Characters read before calls stop | 80,000 |
| One answer | 20,000 characters (2 MB from the server) |
| One call | 45 seconds |
| The run-start listing | 10 seconds, reused for 60 seconds |

Tools past a run limit are left out of that run, with one activity line.
