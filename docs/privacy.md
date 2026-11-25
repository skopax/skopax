# What reaches the AI provider

Skopax sends text to one AI provider (Anthropic or OpenAI, see [The AI provider and its key](setup.md#the-ai-provider-and-its-key)) when it investigates a card, answers a Slack ask, a chat message or a page ask, checks whether a Slack message is meant for it (the topic gate), writes small talk, summarises a Slack thread, writes a self-review or scans a page. This page says what is masked before that text leaves, and what is not.

## What is sent

- Card fields (service, label, numbers, notes, the thread summary) and the board data the investigator reads with its own tools: other cards, past occurrences, past runs, deploys and check tables.
- What the read tools of plug-ins and outside MCP servers return: log lines, Kubernetes state, metrics, code files, documents, query rows and so on.
- What people write: Slack asks and the thread lines around them, chat messages and earlier chat answers, Slack direct messages and page asks.
- Images people attach to a Slack ask (up to three) and screenshots of your public pages that the investigator takes.

## What is masked

The same masking runs on every read tool answer of the investigator's own tools and of plug-in read tools (unless a plug-in switches its tool's scan off) before the model sees it; answers of outside MCP servers get the same rules on their text after the connection's own credentials are removed. It also runs on people's text before it is put in a prompt or stored. The stored copy of an ask or chat message in Skopax is the masked text, so the page shows what the model saw.

- Email addresses, phone numbers, IBANs, card numbers (13 to 19 digits that pass the Luhn check, as text or as a number in a JSON answer) and national identity numbers that pass their checksum.
- Bearer tokens, JWTs and the value after `Authorization:` (Basic and other schemes), cookie headers, `x-api-key`, `api-key` and session values.
- Bare tokens and keys that need no key word in front: Slack, GitHub, GitLab, OpenAI and Anthropic style keys, AWS access key ids, Google API keys, Stripe style live and test keys, and private key blocks.
- In tool answers also `password=...`, `token: ...` and other secret-named values (in English and in the built-in languages), table columns under a secret header, passwords in URLs and the instance's own credentials.

Masking aims to keep ids readable: API-token names of people's actions (such as `token:deploy-bot`), commit SHAs, UUIDs, trace ids, pod names and ordinary numbers such as counts, durations, bytes and timestamps are left alone. A card key that the model sees masked still works when the model passes it back to a tool in the same run.

Plug-ins can add their own patterns (`redactions` on the plug-in, each a `Redaction` with an id and a pattern); they run after the built-in ones on every masked text.

Rows that Skopax stored before 0.5.18 (earlier asks, chat messages and answers) stay as they were; when an earlier chat is continued, its messages are masked as they are given to the model.

## What is not masked

- Images: screenshots in Slack asks are sent to the model as they are, since an image cannot be masked. Skopax's own page screenshots show public pages without cookies.
- Numbers that do not look like a card number or another masked shape, such as amounts, order ids or counts.
- Free text that is not a secret or a masked shape: names, addresses, error messages, customer names in a log line or a ticket summary.
- The card brief of the card under investigation: its label can carry the start of a log line. Use `message-mask` on the Elasticsearch instance to mask parts of log lines before they become card labels.

## What is never read

- Kubernetes Secrets, ConfigMaps and the environment values of pods: the Kubernetes reads are a fixed list (pods, events, rollouts, previous logs, nodes, daemon sets) and only status fields are kept.
- ES|QL queries read only the indices set on the Elasticsearch connection (the log index and the log flow index on cluster logs; the APM index, `logs-apm.error-*` and `metrics-apm.*` on cluster apm), the indices set on enabled plug-in instances that read through that connection (such as CSP reports and Varnish), or narrower patterns of them. Any other index, alias or remote cluster in FROM or LOOKUP JOIN is refused, and so are ENRICH and comments in a query.

## Tracing

Langfuse tracing of Skopax's own AI calls is off unless switched on, and sends no content by default. With `full` content it applies the same masking to every prompt, answer, tool input and output, error and card key before export; see `LANGFUSE_TRACE_CONTENT` in [Configuration](configuration.md#settings).
