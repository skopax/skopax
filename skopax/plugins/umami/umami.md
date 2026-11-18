Umami is the web analytics of the watched website. It counts only the visitors its tracker records, so read its numbers as a trend between windows, never as a total.

Cards of the `umami` check (every 15 minutes, the last hour against the same hour 1 and 7 days earlier):

- `umami-pageviews`: pageviews fell. With the site up, the tracker usually stopped loading: its script route, a consent banner or a site release.
- `umami-redirect` with sub `<event>`: the tracked event named on the instance fell as a whole.
- `umami-redirect` with sub `<event>:<value>`: the events with one value of the first listed property fell.
- A drop card ends with the latest Google Tag Manager publish of the 48 hours before it, when a GTM instance saw one: a new tag or consent setup there can stop the tracker without a site release.
- `umami-access`: Umami refused the website's reads (HTTP 401 or 403). The other cards stay open until it can read again; the tool fails the same way, so fix the API key or the account first.

How to read a card with the `umami` tool:

1. `op stats` with `compare week` for the hour of the card shows whether pageviews fell too.
2. `op event_data` with `compare week` splits the event by one of the listed properties; one value down alone points at that page, product or campaign, all values down at the tracker or the site.
3. `op metrics` with `type path` and `compare day` shows which pages lost their visitors.
4. Check recent deploys of the site around the start of the drop.

The tool reads only the event properties listed on the instance; ids and session data are never read.
