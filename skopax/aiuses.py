import datetime as dt
from collections.abc import Callable, Iterable

from skopax import advisor
from skopax.ai import COMPAT, UNDECLARED, company_of, label_of, provider_of_ref
from skopax.investigator import pricing
from skopax.messages import msg

DAYS = 7
CARD_USES = ('card', 'critical', 'high')
TOOL_USES = CARD_USES + ('slack', 'chat')
GUARD = 'guard'
PLUGIN_MODEL_KEY = 'website.model'
USES = (('card', 'Card runs', 'investigate.model'), ('critical', 'Critical cards', 'investigate.model-critical'), ('high', 'High cards', 'investigate.model-high'),
        ('slack', 'Slack answers', 'investigate.slack-model'), ('chat', 'Chat', 'chat.model'), ('gate', 'Gate and thread summaries', 'investigate.gate-model'),
        ('review', 'Daily self-review', 'self-review.model'), ('plugin', 'Plug-in AI', PLUGIN_MODEL_KEY), (GUARD, 'Guard', 'investigate.guard-model'))
LABELS = {use: label for use, label, _ in USES}
SOURCE_USES = {'pod': 'card', 'page': 'card', 'assess': 'card', 'slack': 'slack', 'chat': 'chat', 'mcp': 'chat', 'gate': 'gate', 'smalltalk': 'gate', 'threadsummary': 'gate',
               'selfreview': 'review', 'pagescan': 'plugin', GUARD: GUARD}


def use_of(row: dict) -> str | None:
    if row.get('use') in LABELS:
        return row['use']
    source = str(row.get('source') or '')
    return SOURCE_USES.get(source) or ('plugin' if '.' in source else None)


def averages(rows: Iterable[dict]) -> dict[str, float]:
    runs: dict[str, dict[str, float]] = {}
    for n, row in enumerate(rows):
        use = use_of(row)
        if use is None:
            continue
        run = str(row.get('runId') or row.get('guardRun') or f'row-{n}')
        held = runs.setdefault(use, {})
        held[run] = held.get(run, 0.0) + float(((row.get('usd') or {}).get('total')) or 0.0)
    return {use: round(sum(costs.values()) / len(costs), 4) for use, costs in runs.items() if costs}


def since(now: float) -> str:
    return (dt.datetime.fromtimestamp(now, dt.UTC) - dt.timedelta(days=DAYS - 1)).strftime('%Y-%m-%d')


def problem_of(use: str, provider: str, model: str, fallback: str | None, default: str, caps: frozenset[str] = UNDECLARED, keyed: Callable[[str], bool] | None = None) -> str | None:
    if fallback and not pricing.known(fallback):
        return msg('ai.model_unknown_fallback', model=fallback, used=model)
    if fallback:
        return msg('ai.model_no_key', model=fallback, owner=label_of(provider_of_ref(fallback)), provider=label_of(default), used=model)
    if not model:
        return None if use == GUARD else msg('ai.model_empty')
    if keyed is not None and provider != default and not keyed(provider):
        return msg('ai.use_held', owner=label_of(provider))
    if not pricing.known(model):
        return msg('ai.model_unknown', model=model)
    if use in TOOL_USES and 'tools' not in caps:
        return msg('ai.no_tools')
    return None


def rows(routes: dict[str, tuple[str, str, str | None]], default: str, ledger: Iterable[dict], caps: Callable[[str], frozenset[str]] = lambda provider: UNDECLARED,
         keyed: Callable[[str], bool] | None = None, settings=None, problems: dict[str, str | None] | None = None) -> list[dict]:
    spent = averages(ledger)
    out = []
    for use, label, setting in USES:
        provider, model, fallback = routes[use]
        out.append({'use': use, 'label': label, 'setting': setting, 'model': model, 'provider': provider, 'company': company_of(model, provider), 'price': pricing.price_of(model),
                    'avgCost7d': spent.get(use), 'problem': (problems or {}).get(use) or problem_of(use, provider, model, fallback, default, caps(provider), keyed),
                    'fit': 'cheap' if use in CARD_USES and provider.startswith(COMPAT + ':') else 'ok',
                    'advisor': advisor.view(settings, use, provider, caps(provider), model) if settings is not None and use in advisor.USES else None})
    return out


def notices(found: list[dict], default: str, keyed: Callable[[str], bool] | None = None) -> list[dict]:
    def held(r: dict) -> bool:
        return keyed is not None and bool(r['model']) and r['provider'] != default and not keyed(r['provider'])
    def unknown(r: dict) -> bool:
        return str(r['provider']).startswith('vertex:') and bool(r['model']) and not pricing.known(r['model'])
    return [{'id': f"ai-use:{r['use']}", 'level': 'warn', 'text': msg('ai.use_problem', use=r['label'], problem=problem_of(r['use'], r['provider'], r['model'], r['fallbackFrom'], default, keyed=keyed))}
            for r in found if r.get('fallbackFrom') or held(r) or unknown(r)]
