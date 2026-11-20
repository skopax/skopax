import re

MORE = 'More'
PLUGIN_RULES = 'Thresholds'
PLUGIN_OWN = 'Settings'

CARDS: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    'General': (
        ('Language and instances', ('core.language', 'core.instance-links')),
        ('Time', ('general.timezone',)),
        ('Logs', ('core.log-format', 'core.log-color', 'core.log-level')),
    ),
    'Access': (
        ('Reading', ('access.public-read',)),
    ),
    'Investigator': (
        ('Provider', ('investigate.provider',)),
        ('Models', ('investigate.model', 'investigate.effort', 'investigate.slack-model', 'investigate.slack-effort', 'chat.model', 'investigate.gate-model', 'investigate.gate-effort')),
        ('Spend and limits', ('investigate.daily-usd', 'page.ask-cap', 'chat.max-chars', 'chat.daily-cap', 'mcp.dive-cap', 'investigate.max-cards', 'investigate.rooms',
                              'investigate.max-tool-calls', 'investigate.assess-max-tool-calls', 'investigate.slack-max-tool-calls', 'screenshot.max-per-day')),
        ('Web', ('investigate.web-domains', 'investigate.web-searches', 'investigate.web-fetches')),
        ('Behaviour', ('investigate.mode', 'investigate.language', 'investigate.refire-hours', 'investigate.strict-tools', 'core.org-about', 'mcp.wait-seconds')),
    ),
    'Slack': (
        ('Channel and posting', ('slack.channel', 'slack.post-sev', 'slack.fallback-minutes', 'routes.slack-echo', 'slack.attach-screenshots', 'slack.push-user')),
        ('Questions', ('slack.ask-cap', 'slack.user-ask-cap', 'slack.ask-channels', 'slack.dm-chats', 'slack.listen', 'slack.operator-cache-seconds')),
        ('Threads and reminders', ('slack.thread-hours', 'slack.remind-hours', 'slack.clear-note-minutes', 'slack.state-notes', 'slack.great-emoji')),
    ),
    'Self-review': (
        ('Daily review', ('self-review.model', 'self-review.dm', 'self-review.hour')),
    ),
    'Cards': (
        ('Archive and resolve', ('cards.archive-hours', 'core.auto-resolve-hours-crit', 'core.auto-resolve-hours-warn', 'core.info-auto-close-days')),
        ('Recurring', ('recurring.count', 'recurring.days')),
    ),
    'Jira tasks': (
        ('Projects', ('jira.projects', 'jira.project', 'jira.parent', 'jira.tech-project', 'jira.tech-assignee')),
        ('New tasks', ('jira.points', 'jira.dev-scope')),
        ('Releases', ('jira.release-name',)),
    ),
    'Monitor rules': (
        ('Certificates', ('core.certs', 'core.cert-skip')),
    ),
}
SECTION_CARD = {'Card tags': 'Tag rules', 'Features': 'Switches'}
UNITS = {
    'investigate.daily-usd': '$', 'page.ask-cap': 'per hour', 'slack.ask-cap': 'per hour', 'slack.user-ask-cap': 'per hour', 'mcp.dive-cap': 'per hour', 'chat.daily-cap': 'per day',
    'screenshot.max-per-day': 'per day', 'investigate.web-searches': 'per run', 'investigate.web-fetches': 'per run', 'investigate.max-cards': 'cards',
    'investigate.max-tool-calls': 'calls', 'investigate.assess-max-tool-calls': 'calls', 'investigate.slack-max-tool-calls': 'calls', 'investigate.rooms': 'rooms',
    'chat.max-chars': 'chars', 'investigate.refire-hours': 'h', 'jira.points': 'points', 'slack.operator-cache-seconds': 's',
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
