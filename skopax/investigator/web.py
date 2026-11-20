import logging
import re
from dataclasses import dataclass

from skopax.ai import FETCH_TYPE, SEARCH_TYPE

log = logging.getLogger('skopax.investigator.web')

TYPES = (SEARCH_TYPE, FETCH_TYPE)
NAMES = ('web_fetch', 'web_search')
RESULTS = {'web_search_tool_result': 'web_search', 'web_fetch_tool_result': 'web_fetch'}
FETCH_TOKENS = 20_000
OFF_WORDS = ('off', 'none', 'false', 'no')
ANY = 'any'
DOMAIN = re.compile(r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}')
DOMAINS = ('support.google.com', 'developers.google.com', 'firebase.google.com', 'cloud.google.com', 'status.cloud.google.com', 'blog.google',
           'developer.apple.com', 'developer.android.com', 'developers.cloudflare.com', 'cloudflarestatus.com',
           'kubernetes.io', 'docs.gitlab.com', 'postgresql.org', 'mongodb.com', 'kafka.apache.org',
           'redis.io', 'elastic.co', 'grafana.com', 'prometheus.io', 'langfuse.com', 'help.vwo.com', 'umami.is', 'platform.claude.com')


@dataclass(frozen=True)
class Web:
    domains: tuple[str, ...] | None
    searches: int
    fetches: int

    def definitions(self) -> list[dict]:
        allowed = {'allowed_domains': list(self.domains)} if self.domains else {}
        tools = []
        if self.fetches > 0:
            tools.append({'type': FETCH_TYPE, 'name': 'web_fetch', 'max_uses': self.fetches, 'max_content_tokens': FETCH_TOKENS,
                          'url_sources': {'client_tool_results': {'type': 'none'}}} | allowed)
        if self.searches > 0:
            tools.append({'type': SEARCH_TYPE, 'name': 'web_search', 'max_uses': self.searches} | allowed)
        return tools


def domains_of(value: str) -> tuple[str, ...] | None:
    wanted = [d for d in re.split(r'[\s,]+', value.strip().lower()) if d]
    kept = tuple(dict.fromkeys(d for d in wanted if DOMAIN.fullmatch(d)))
    dropped = [d for d in wanted if not DOMAIN.fullmatch(d)]
    if dropped:
        log.warning('investigate.web-domains: %s left out, only plain host names such as support.google.com work', ', '.join(dropped))
    return kept


def web_from(domains: str, searches: int, fetches: int) -> Web | None:
    value = (domains or '').strip().lower()
    if value in OFF_WORDS or (searches <= 0 and fetches <= 0):
        return None
    if value == ANY:
        return Web(None, max(0, searches), max(0, fetches))
    listed = domains_of(value) if value else DOMAINS
    if not listed:
        log.warning('investigate.web-domains has no usable host name; the web tools stay off')
        return None
    return Web(listed, max(0, searches), max(0, fetches))


def is_web_tool(definition: dict) -> bool:
    return definition.get('type') in TYPES


def part(obj, name: str):
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


def result_of(block) -> tuple[int | None, str | None, str]:
    inner = part(block, 'content')
    if part(block, 'type') == 'web_search_tool_result':
        if isinstance(inner, list):
            return len(inner), None, ' '.join(str(part(r, 'title') or '') for r in inner)
        return None, str(part(inner, 'error_code') or 'error'), ''
    code = part(inner, 'error_code')
    if code or inner is None:
        return None, str(code or 'error'), ''
    source = part(part(inner, 'content'), 'source')
    body = part(source, 'data') if part(source, 'type') == 'text' else ''
    return 1, None, body if isinstance(body, str) else ''
