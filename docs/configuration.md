# Configuration

Settings from the environment, the config file and plug-in sources, branding and languages.

## Settings

- `SKOPAX_SECRET_KEY`: signs login sessions and seals every integration secret (tokens and passwords of plug-in instances). Recommended; set it to a long random value. If unset, a key is generated once and kept in the database. Setting it later seals the stored secrets again with the new key on the next start. If the generated key is lost from the database, the sealed secrets cannot be opened: every secret has to be entered again. Once set, keep the same value: after a change from one value to another, Skopax cannot open the integration secrets sealed with the old one, so each has to be entered again in Admin, and everyone has to sign in again.
- `SKOPAX_ADMIN_EMAIL`, `SKOPAX_ADMIN_PASSWORD`: an admin account created from the environment. They apply only while no active admin account exists; once one does, they are ignored (see [Lost setup code](operations.md#lost-setup-code)).
- `SKOPAX_PUBLIC_READ`: default `false`. When `true`, anyone who can reach the page can read the board without signing in. A value saved on the Read without login card in Admin > Features overrides this setting; see [Feature switches](setup.md#feature-switches) for going back to it.
- `SSO_DEFAULT_ROLE`: the role of an SSO user when no resolver, claim or Admin-set role gives one: `viewer` (default) or `operator`. The other sign-in settings are listed under [Headless setup](sign-in.md#headless-setup).
- `SKOPAX_TOKEN`: an optional bootstrap API token; set it to a long random value (it acts as an admin). Prefer tokens created in Admin > Access & sign-in, which carry their own role and can be revoked. Neither kind opens MCP.
- `FORWARDED_ALLOW_IPS`: see [Behind a proxy or ingress](operations.md#behind-a-proxy-or-ingress).
- `PROMETHEUS_URL`: the Prometheus the board reads. It defines the primary Prometheus instance, named `main`. `PROMETHEUS_UI_URL` is the address people open for its graph links (default: the URL), and `PROMETHEUS_NAMESPACES` is the pattern of the namespaces it watches (default `.*`). Without it, and without an instance added in Admin, the Prometheus checks do not run. See [Plug-ins](plugins.md#plug-ins).
- `SKOPAX_BASE_URL`: public URL of this instance. Slack and Teams posts need an absolute URL for their links, and they always use this base when it is set (or the host of `SSO_REDIRECT_URI` when it is not); only with neither set does a post sent from the page link to the address the request came in on. Cookies are marked Secure when it starts with `https://`. Single sign-on needs it: the redirect URI is this base followed by `/authorized`, unless `SSO_REDIRECT_URI` is set.
- `SKOPAX_SELF_REVIEW`: self-review is off by default; set it to `true` and set `SKOPAX_SELF_PROJECT` to turn it on.
- `SKOPAX_ENV_LABEL`, `SKOPAX_INSTANCE_LINKS`: the instance switcher; see [Other instances](setup.md#other-instances).
- `SKOPAX_FEATURE_*`: starting values for the feature switches; see [Feature switches](setup.md#feature-switches).
- `ANTHROPIC_API_KEY`: the investigator's key, used unless one is set in Admin > Investigator.
- `SKOPAX_TIMEZONE`: timezone for day boundaries and display, default `UTC`.
- `SKOPAX_LOG_FORMAT`, `SKOPAX_LOG_COLOR`, `SKOPAX_LOG_LEVEL`: the log format (`text`, the default, `json` or `log4j`), colour (`always`, the default, `auto` or `never`; a non-empty `NO_COLOR` turns it off, whatever Admin or a settings source says) and level (`DEBUG`, `INFO`, the default, `WARNING` or `ERROR`). Admin > General lists them as Log format, Log colour and Log level, and a value saved there applies at once; the first lines of a start always follow the variables. See [Logs](operations.md#logs).
- `SKOPAX_DISPLAY_NAME`: the name shown to people on the page, in the chat and in Slack; default `Skopax`. The characters `<`, `>`, `&`, `"`, control characters (including the C1 range) and unpaired surrogates are dropped and the name is cut at 40 characters. The name also names the MCP server, as a lowercase slug of letters, digits and hyphens, so changing it renames the tools in connected clients.
- `SKOPAX_LANGUAGE`: the outbound language, default `en`: the language of what Skopax sends to Slack, Teams, Jira and direct messages. It is `en` or a language that a serving plug-in's locale pack adds; see [Languages](#languages). Admin > General shows it as Outbound language.
- `SKOPAX_ORG_ABOUT`: one or two sentences on what Skopax watches, read by the gate that screens Slack messages; at most 300 characters. Admin > Investigator shows it as About your company, and a value saved there applies at once.
- `SKOPAX_LINK_HOSTS` (`core.link-hosts`): comma-separated hosts that Skopax may link to in what the investigator posts, next to its own address and the addresses of your plug-in instances. An entry such as `example.com` also allows every subdomain, `docs.example.com` included. Write `=example.com` to allow that host only: `sub.example.com`, `example.com.evil.test` and `evilexample.com` are then refused. Another source of the same host, such as a plain entry or a plug-in's address, keeps its wider reach.
- `SKOPAX_PROBE`: default `true`. Switches the reachability probe; see [Probe](operations.md#probe). Admin > Features lists it under Other switches as Reachability probe, and a value saved there overrides this one and applies at once.
- `SKOPAX_CHAT_ATTRIBUTION`: default `false`. When `true`, every message Skopax starts in Slack and Teams ends with the "powered by" line: its channel posts (on a card post, as the last item of the bottom bar) and its own direct messages (phone push, assignee message, self-review). A reply to a person never carries it: no thread reply, no answer, small-talk line or "thinking" note in a direct message. The Enterprise switch Hide "powered by Skopax" wins over it. Admin > Features lists it under Other switches as Powered by on Slack and Teams posts, and a value saved there overrides this one and applies at once.
- `SKOPAX_CERT_RULES`: the thresholds of the certificate cards as JSON, default `{"warn_days": 21, "crit_days": 7}`. Admin > General lists them under Monitor rules as Certificates. Certificate cards are made for HTTPS probe targets whose certificate verified, and an expired certificate still gets its critical card. Probe target URLs and errors are shown to admins only.
- `SKOPAX_CERT_SKIP`: hosts whose certificate raises no card, separated by commas. `shop.example.com` also covers its subdomains, `*.example.com` covers only the subdomains. Admin > General lists it under Monitor rules as Certificates not watched. A skipped host still counts as checked, so a card it already has closes on the next probe.
- `SKOPAX_AUTO_RESOLVE_HOURS` (`core.auto-resolve-hours`): default `6`, from 0 to 720, where 0 turns it off. A card that stopped firing and that nobody acted on (nobody took it, linked a ticket, muted it or sent the investigator) is closed as auto-resolved after this many hours; a card that fires again inside the window is not closed. Its note and the Slack thread line say "Did not recur for 6 hours", or whole days ("3 days") when the hours divide by 24. Admin > Cards lists it as Auto-resolve cleared cards after (hours). Plug-ins can give some card kinds a window of their own, counted from when the card first appeared: VWO A/B result cards and Google Ads disapproved-ads cards close after 24 hours by default (`vwo.auto-resolve-hours`, `google-ads.auto-resolve-hours`; 0 uses this general rule). A card of such a kind follows its own window and not this one.
- `SKOPAX_AUTO_RESOLVE_DAYS` (`core.auto-resolve-days`): deprecated, replaced by the hours setting and no longer listed in Admin. It is still read: when it is set (in the environment, the config file or an earlier Admin value) and the hours are not, the hours are the days times 24; when both are set, the hours win; 0 days turns auto-resolve off.

This list names the settings most installs set. Every setting has an id, such as `core.timezone`, shown under its row in Admin > General (or in the Admin section that owns it). To set one without the page, give it under `settings:` in the config file (see [Settings sources](#settings-sources)), where `${NAME}` takes its value from the environment.

The compose file publishes the port on 127.0.0.1 only. It passes exactly five variables through from your shell environment or `.env`: `SKOPAX_TOKEN`, `SKOPAX_SECRET_KEY`, `SKOPAX_ADMIN_EMAIL`, `SKOPAX_ADMIN_PASSWORD` and `SKOPAX_PUBLIC_READ`; any other variable there never reaches the container. Any other setting, for example `FORWARDED_ALLOW_IPS` behind a proxy or `SSO_DEFAULT_ROLE`, goes under the `skopax` service's `environment:` in `docker-compose.yml`.

The compose file sets `SKOPAX_BASE_URL` to `http://localhost:8080`; change it for a real deployment. Cookies are `Secure` when that base (or `SSO_REDIRECT_URI` if no base is set) starts with `https://`.

## Branding

A small attribution line links to skopax.io. It reads "Skopax" and the version by default, or the display name followed by "powered by Skopax" and the version once `SKOPAX_DISPLAY_NAME` is set. On the board it sits at the foot of the sidebar, and at 768 px wide or less it also ends the content. The card page, the login and setup forms and the chat show it as a line at the foot. Slack and Teams messages carry it only when `SKOPAX_CHAT_ATTRIBUTION` is on (off by default; see [Settings](#settings)): every message Skopax starts ends with it, never a reply to a person, and the hide switch below wins over it. Under Apache-2.0 anyone may edit the line out of their own copy. Admin > Features also has a ready-made switch, Hide "powered by Skopax", that needs the Enterprise entitlement; without it the switch stays locked and the line stays, whatever the environment, the config file or a settings source say. The Skopax name and logo stay trademarks.

## Settings sources

Settings and instances come from these sources, in this order:

1. the environment (for example `PROMETHEUS_URL`);
2. the file named by `SKOPAX_CONFIG_FILE`, default `/etc/skopax/skopax.yaml`, read again every minute;
3. sources supplied by plug-ins;
4. Admin.

For a setting, a later source overrides an earlier one. The file may hold only `settings` and `plugins`, and `${NAME}` or `${NAME:default}` in a value is filled from the environment (plain `${NAME}` stops the start if the variable is unset):

    settings:
      core.timezone: ${TZ_NAME:UTC}
    plugins:
      prometheus:
        instances:
          - name: east
            url: http://prometheus-east.example.com:9090

If two sources define an instance with the same name, the first one wins.

A bad value in the environment or the file stops the start and names the field. A bad instance from a plug-in source is parked with a reason shown in Admin, and the rest keeps running. If a source fails later, the last good values stay in use. Instances from a source are shown in Admin but edited at their source.

### Spring Cloud Config

The Spring Cloud Config plug-in is a settings source of the third kind: it reads a Spring Cloud Config server and turns chosen keys into Skopax settings and instance fields. It has no form in Admin; it is set by environment variables, and without `SPRING_CONFIG_URL` it supplies no source at all (a settings source hook that returns nothing is skipped).

- `SPRING_CONFIG_URL` (the server address, `http` or `https`, with no credentials or query in it), `SPRING_CONFIG_APP` (the application name), `SPRING_CONFIG_PROFILE` (default `default`; a comma list is allowed) and `SPRING_CONFIG_LABEL` (the branch or tag; empty leaves it to the server; a `/` in it is sent as `(_)`). Skopax reads `<url>/<app>/<profile>/<label>`.
- `SPRING_CONFIG_USER` and `SPRING_CONFIG_PASSWORD`: a basic sign-in, both or neither.
- `SPRING_CONFIG_MAP` (required): the map from config keys to Skopax, a comma list of `<config key>=<target>`. A target is a setting id, such as `investigate.max-cards`, or an instance field written `<plug-in id>/<instance name>/<field>`, such as `jira/main/url`; the fields given for one instance name make one instance. A config key and a target can each appear once, a target may not be an instance's `plugin` or `name`, and the old connection keys (`jira.url`, `gitlab.token` and the others core does not read as settings) are refused; any of these stops the start, and the log names the entry. Keys of the server that the map does not name are ignored.
- **Values:** the answer's `propertySources` are read in order and the first source that has a key wins. In a mapped value, `${key}` and `${key:default}` are filled from the config (all sources), then from the environment (the name as written, then in upper case with every other character as `_`), then from the default. The environment lookup reads the whole environment of the Skopax process, so a placeholder naming a secret variable would copy that secret into the mapped setting; keep the config server as trusted as the environment. A null value in the config counts as not there. A placeholder that cannot be filled, one that loops (`${a}` to `${b}` to `${a}`) or an expansion that grows too large leaves that one value out with one log warning that names the config key and the placeholder, never a value. A mapped key the server does not have is left out with a warning too.
- **Time and failure:** one request, with a limit of 8 seconds in all, under the 10 seconds a source hook may take. The answer is kept and read again every 15 minutes; after a failed read the next try waits 5 minutes, and the last good copy stays in use. A main that cannot read the server at start does not start, and the error names the cause (for example `Unauthorized`, `TimedOut`, `EmptyAnswer`); an agent starts on its environment alone and tries again. An answer with no property source counts as a failure, so a mistyped application name does not wipe the settings it gave before.
- Passwords and secret values are never logged.

## Languages

Skopax speaks English; other languages come from locale packs (see [Locale packs](extending.md#locale-packs)). Each place picks its language in its own way:

- **What Skopax sends.** Slack and Teams posts, Jira tickets and direct messages use the outbound language: Outbound language in Admin > General, or `SKOPAX_LANGUAGE`. It offers `en` and each language that a serving plug-in's pack adds, and a regional tag such as `pt-BR` must match a pack's tag. A saved language whose pack has gone stays shown with a hint, and English is sent until a pack adds it again. A text that the pack lacks is sent in English.
- **Replies that follow the asker.** The reply to a Slack question that is off-topic, or over the small-talk or ask limit, is in the language the topic gate reads from the question when a pack has it, including a regional pack for the plain language (`pt` reaches `pt-BR`); otherwise it is in English. A reply sent without a gate verdict is in the outbound language.
- **The investigator.** It writes cards, Slack drafts and deploy reports in the Card language (Admin > Investigator, or `INVESTIGATE_LANGUAGE`), which offers `en` and each language whose pack has a writing note. It answers a question in the language of the question.
- **The page and the API stay English.** Page packs translate the page in the browser, the card page included. Under Settings > Appearance > Language, English keeps the page in English. Auto walks the browser's languages in order and takes, for each, its exact tag, then its plain language or a regional pack of it (`pt-PT` reaches `pt-BR`); an English entry ends the walk in English. A chosen language that no pack serves falls through to the browser's languages the same way. Card data, such as titles, texts and names, values inside a translated message, such as an error reason, and everything the API returns stay as written.
- **The chat** uses a pack's chat texts, picked the same way as the page, so the board and the chat speak one language when both have a pack for it. Chainlit's own buttons follow when Chainlit ships that language, and are in English when it does not.
