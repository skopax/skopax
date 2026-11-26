import re

from skopax import advisor

MORE = 'More'
PLUGIN_RULES = 'Thresholds'
PLUGIN_OWN = 'Settings'

CARDS: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    'General': (
        ('Language', ('core.language',)),
        ('Time', ('general.timezone',)),
        ('Logs', ('core.log-format', 'core.log-color', 'core.log-level')),
    ),
    'Workspaces': (
        ('Other workspaces', ('core.workspace-links',)),
        ('Workspace icon', ('core.workspace-icon',)),
    ),
    'Access': (
        ('Reading', ('access.public-read',)),
    ),
    'Investigator': (
        ('Provider', ('investigate.provider', 'core.allow-private-ai')),
        ('Models', ('investigate.model', 'investigate.effort', 'investigate.model-critical', 'investigate.model-high', 'investigate.slack-model', 'investigate.slack-effort', 'chat.model', 'chat.effort', 'investigate.gate-model', 'investigate.gate-effort',
                    'investigate.guard-model', 'guard.check-questions', 'guard.check-answers')),
        ('Advisor models', tuple(key for found in advisor.keys().values() for key in found)),
        ('Spend and limits', ('investigate.daily-usd', 'page.ask-cap', 'chat.max-chars', 'chat.daily-cap', 'mcp.dive-cap', 'investigate.max-cards', 'investigate.rooms',
                              'investigate.max-tool-calls', 'investigate.assess-max-tool-calls', 'investigate.slack-max-tool-calls', 'chat.max-tool-calls', 'screenshot.max-per-day')),
        ('Web', ('core.own-sites', 'investigate.web-domains', 'investigate.web-searches', 'investigate.web-fetches')),
        ('Behaviour', ('investigate.mode', 'investigate.language', 'investigate.refire-hours', 'investigate.strict-tools', 'core.org-about', 'mcp.wait-seconds')),
        ('Follow-up checks', ('investigate.follow-up-hours', 'investigate.follow-up-days', 'investigate.follow-up-close-hours', 'investigate.settle-max-per-day')),
        ('Langfuse tracing', ('langfuse.trace-instance', 'langfuse.trace-content', 'langfuse.trace-sample')),
    ),
    'Slack': (
        ('Channel and posting', ('slack.channel', 'slack.post-sev', 'slack.fallback-minutes', 'routes.wait-minutes', 'routes.slack-echo', 'slack.attach-screenshots', 'slack.push-user')),
        ('Questions', ('slack.ask-cap', 'slack.user-ask-cap', 'slack.ask-channels', 'slack.dm-chats', 'slack.thread-summary', 'slack.listen', 'slack.operator-cache-seconds')),
        ('Threads and reminders', ('slack.thread-hours', 'slack.remind-hours', 'slack.clear-note-minutes', 'slack.state-notes', 'slack.great-emoji')),
        ('Daily warnings', ('slack.warn-digest-at',)),
    ),
    'Self-review': (
        ('Daily review', ('self-review.model', 'self-review.dm', 'self-review.hour')),
    ),
    'Cards': (
        ('Archive and resolve', ('cards.archive-hours', 'core.auto-resolve-hours-crit', 'core.auto-resolve-hours-high', 'core.auto-resolve-hours-warn', 'core.business-days')),
        ('Recurring', ('recurring.count', 'recurring.days')),
        ('Alerts', ('alerts.warn',)),
    ),
    'Jira tasks': (
        ('Projects', ('jira.projects', 'jira.project', 'jira.parent', 'jira.tech-project', 'jira.tech-assignee')),
        ('New tasks', ('jira.points', 'jira.dev-scope', 'jira.sprint-default')),
        ('Fields', ('jira.scope-field', 'jira.points-field', 'jira.developer-field', 'jira.sprint-field')),
        ('Releases', ('jira.release-name',)),
        ('Back-links', ('jira.backlink',)),
    ),
    'Monitor rules': (
        ('Certificates', ('core.certs', 'core.cert-skip')),
        ('Reachability', ('core.probe-extra',)),
    ),
    'Card tags': (
        ('Daily report', ('tags.report-at',)),
        ('Mail', ('tags.mail',)),
    ),
}
SECTION_CARD = {'Card tags': 'Tag rules', 'Features': 'Switches'}
UNITS = {
    'investigate.daily-usd': '$', 'page.ask-cap': 'per hour', 'slack.ask-cap': 'per hour', 'slack.user-ask-cap': 'per hour', 'mcp.dive-cap': 'per hour', 'chat.daily-cap': 'per day',
    'screenshot.max-per-day': 'per day', 'investigate.web-searches': 'per run', 'investigate.web-fetches': 'per run', **{key: 'per run' for found in advisor.keys().values() for key in found[1:2]}, **{key: 'tokens' for found in advisor.keys().values() for key in found[2:3]}, 'investigate.max-cards': 'cards',
    'investigate.max-tool-calls': 'calls', 'investigate.assess-max-tool-calls': 'calls', 'investigate.slack-max-tool-calls': 'calls', 'chat.max-tool-calls': 'calls', 'investigate.rooms': 'rooms',
    'chat.max-chars': 'chars', 'investigate.refire-hours': 'h', 'investigate.follow-up-hours': 'h', 'investigate.follow-up-close-hours': 'h', 'investigate.settle-max-per-day': 'per day', 'investigate.follow-up-days': 'days', 'jira.points': 'points', 'slack.operator-cache-seconds': 's', 'langfuse.trace-sample': '%',
}
LABEL_UNITS = {'h': 'h', 'hours': 'h', 'min': 'min', 'minutes': 'min', 's': 's', 'seconds': 's', 'days': 'days', '$': '$', 'times': 'times', 'characters': 'chars'}
LABEL_UNIT = re.compile(r'\(([^()]+)\)\s*$')
NUMBERS = ('int', 'float')
CARD_OF = {key: (section, title, n * 100 + k) for section, cards in CARDS.items() for n, (title, keys) in enumerate(cards) for k, key in enumerate(keys)}
LOOSE = 9000


def unit_of(t) -> str | None:
    if t.kind not in NUMBERS:
        return None
    if t.key in UNITS:
        return UNITS[t.key]
    found = LABEL_UNIT.search(t.label)
    return LABEL_UNITS.get(found.group(1).strip()) if found else None


def card_of(t) -> str:
    found = CARD_OF.get(t.key)
    if found and found[0] == t.group:
        return found[1]
    if t.plugin is not None:
        return PLUGIN_RULES if t.kind == 'rules' else PLUGIN_OWN
    return SECTION_CARD.get(t.group, MORE)


def order_of(t) -> int:
    found = CARD_OF.get(t.key)
    return found[2] if found and found[0] == t.group else LOOSE


def short_label(t, unit: str | None) -> str | None:
    found = LABEL_UNIT.search(t.label)
    if not unit or not found or found.group(1).strip() not in LABEL_UNITS:
        return None
    return t.label[:found.start()].rstrip()


def placed(t) -> dict:
    unit = unit_of(t)
    short = short_label(t, unit)
    return {'card': card_of(t), 'order': order_of(t), **({'unit': unit} if unit else {}), **({'shortLabel': short} if short else {})}
