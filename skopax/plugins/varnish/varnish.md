The Varnish checks read Varnish access logs from an Elasticsearch instance with ES|QL. Use the esql tool on that instance to look further.

The logs carry one document per request. These fields are required; without them the checks read nothing and raise a card that names the missing ones:
- varnish.method.keyword: the request method, such as GET.
- varnish.cache_status.keyword: hit for a cache hit; any other value counts as a miss.
- varnish.path.keyword: the request path without its query string.

These fields are optional; without them the path list is still read, and only the hints that need them are left out:
- varnish.query_string: the query string, when the request had one.
- varnish.query_params._rsc.keyword: the _rsc query value of Next.js RSC requests, when there is one.
- varnish.response_code.keyword: the status code the client got, such as 200.

What the scheduled checks read, so you can reproduce a card:
- Hit ratio (varnish): the share of GET requests with a cache status that hit the cache in the last 60 minutes, against the same hour one day and one week earlier. A card opens when the share falls below the drop share of both earlier hours. It names up to three paths that missed most in the last hour.
- Cache candidates (varnish-cache): once a day, the paths with the most misses in the last 24 hours, each with a hint why it misses: a personal flow, non-200 answers, a static file, RSC requests, unique query values, or a page the VCL does not cache.

Query tips:
- Filter on varnish.method.keyword == "GET" and varnish.cache_status.keyword IS NOT NULL, as the checks do.
- Count misses with CASE(varnish.cache_status.keyword == "hit", 0, 1) and group with STATS ... BY path = varnish.path.keyword.
- Bound every query with a @timestamp condition, such as WHERE @timestamp >= NOW() - 1 hour.
