Use edge and cloudflare_graphql for questions about traffic at the Cloudflare edge that the cards and the Cloudflare page cannot answer. Both read the zones of one Cloudflare instance; when several are configured, pass the instance input to pick one, otherwise the primary answers.

Read first what the checks saw, with no outside call:
- `pass_table` source `edge`: per watched zone, the requests of the last complete hour (or the running one), the cached share, the 502 / 52x and other 5xx counts and shares, and the same shares a day and a week earlier. Each row also holds the zone tag that cloudflare_graphql needs.
- `pass_table` source `cloudflare`: the health checks (part `checks`), certificate packs (part `certs`) and watched status-page components (part `components`), and a summary row.
- A `no-access` card of the cf or edge check means the token is invalid or lacks a permission: the part it names (zones, health checks, certificates, analytics) is not read, and the cards of that part are neither opened nor closed until the token gets it.

How the edge check reads the numbers:
- It counts 502 and 520-526 apart from the other 5xx (500, 503, 504, 530) and leaves 499 and every 4xx out.
- A 502 or 520-526 that the edge makes never reaches the origin, so origin logs and origin volume cannot rule out an edge problem. The card text says which share the origin sent (sampled), and the top hosts and paths behind it.
- A zone under the `min_req` rule of `cloudflare.edge` is not judged: a share of a handful of requests says nothing.
- The hour columns are UTC hours; the 7 day column comes from a sampled dataset.

Then, for live traffic:
- `edge` op `status` on one host gives requests per 5-minute bucket by edge status and by cache status; compare it with the edge table for the hour of a card.
- `edge` op `top` with status 5xx, 4xx or one code gives the codes, top paths, client ASNs and countries behind them.
- `cloudflare_graphql` only for what `edge` cannot answer: other datasets (firewall events, Workers, load balancing), other dimensions or several hosts at once. Keep the window to 24 h or less, set `limit`, and filter by zone tag.

In a Cloudflare incident, the edge numbers decide. The Cloudflare status page names the affected components; CDN/Cache and Network are the request path, Dashboard and API are the control plane. A status card names the watched component and how Cloudflare rates it.

The instance field zone-checks can limit a zone to the cf checks, the edge check or neither; a zone without a row in one table is not read by that check by choice, not because it failed.
