import logging
import re
from collections.abc import Callable

from skopax import brand, clock, jirafields
from skopax.errkind import NoAccess, text_of
from skopax.engine import parse_iso
from skopax.enrich import JiraRefused
from skopax.investigator.prompt import card_link
from skopax.jiralinks import ticket_pattern
from skopax.messages import msg
from skopax.store import Store

log = logging.getLogger('skopax.tickets')
TITLE_LIMIT = 200
SUSPECTS = 5
LINKS = 10
FIBONACCI = (1, 2, 3, 5, 8, 13, 21)
OPTION_KINDS = ('option', 'options')
PROJECT_KEY = re.compile(r'[A-Z][A-Z0-9]{1,9}')


def default_title(card: dict, prefix: str = '') -> str:
    return f"{prefix}{card.get('svc') or '?'} {card.get('label') or card.get('key')}"[:TITLE_LIMIT]


def paragraph(text: str) -> dict:
    return {'type': 'paragraph', 'content': [{'type': 'text', 'text': text}]}


def link_paragraph(label: str, url: str, lead: str = '') -> dict:
    content = ([{'type': 'text', 'text': lead}] if lead else []) + [{'type': 'text', 'text': label, 'marks': [{'type': 'link', 'attrs': {'href': url}}]}]
    return {'type': 'paragraph', 'content': content}


def code_block(text: str, language: str | None) -> dict:
    return {'type': 'codeBlock'} | ({'attrs': {'language': language}} if language else {}) | {'content': [{'type': 'text', 'text': text}] if text else []}


def fixes(card: dict) -> list[dict]:
    out = []
    for c in card.get('code') or []:
        if not (isinstance(c, dict) and isinstance(c.get('current'), str) and c.get('suggestion') and str(c.get('url') or '').startswith('https://')):
            continue
        out += [{'type': 'paragraph', 'content': [{'type': 'text', 'text': 'Suggested fix (untested)', 'marks': [{'type': 'strong'}]}]},
                link_paragraph(f"{c.get('repo') or '?'} · {c.get('path') or c.get('label') or '?'}:{c.get('lines') or '?'} @ {c.get('sha') or 'master'}", str(c['url']))]
        if c.get('note'):
            out.append(paragraph(str(c['note'])))
        out += [paragraph('Current:'), code_block(c['current'], c.get('language')), paragraph('Suggested:'), code_block(str(c['suggestion']), c.get('language'))]
    return out


def links_of(card: dict) -> list[tuple[str, str]]:
    out = []
    for item in list(card.get('links') or []) + list(card.get('code') or []):
        if isinstance(item, dict) and item.get('url') and str(item['url']).startswith('https://'):
            out.append((str(item.get('label') or 'link')[:120], str(item['url'])))
    return out[:LINKS]


def tail(card: dict, link: str) -> list[dict]:
    return [link_paragraph(brand.product(), link, msg('tickets.card'))] + [link_paragraph(label, url, msg('tickets.link')) for label, url in links_of(card)]


def typed_description(card: dict, text: str, link: str) -> dict:
    parts = [paragraph(block.strip()) for block in text.replace('\r', '').split('\n') if block.strip()]
    return {'type': 'doc', 'version': 1, 'content': parts + fixes(card) + tail(card, link)}


def description(card: dict, note: str, link: str) -> dict:
    parts = [paragraph(f"{card.get('sev') or '?'} · {card.get('svc') or '?'} · {card.get('label') or ''} · now {card.get('now')} vs 1 d {card.get('d1')} / 7 d {card.get('d7')} · since {clock.date(parse_iso(card['firstSeen'])) + ' ' + clock.label(parse_iso(card['firstSeen'])) if card.get('firstSeen') else '?'}")]
    if card.get('short'):
        parts.append(paragraph(str(card['short'])))
    if note:
        parts.append(paragraph(note))
    stored = (card.get('note') or {}).get('text')
    if stored and stored != note:
        parts.append(paragraph(msg('tickets.findings', text=stored)))
    if card.get('suspects'):
        parts.append(paragraph(msg('tickets.suspects', list='; '.join(str(s) for s in card['suspects'][:SUSPECTS]))))
    if card.get('recommend'):
        parts.append(paragraph(msg('tickets.recommended', text=card['recommend'])))
    return {'type': 'doc', 'version': 1, 'content': parts + fixes(card) + tail(card, link)}


def projects_of(settings) -> list[str]:
    raw = getattr(settings, 'jira_projects', ()) or ()
    listed = [p.strip().upper() for p in (raw.split(',') if isinstance(raw, str) else raw)]
    return list(dict.fromkeys(p for p in [settings.jira_project] + listed if PROJECT_KEY.fullmatch(p or '') and p != settings.jira_tech_project))


def known_accounts(value: str) -> dict[str, str]:
    out = {}
    for pair in (value or '').replace(';', ',').split(','):
        login, _, account = pair.partition('=')
        if login.strip() and account.strip():
            out[login.strip().lower()] = account.strip()
    return out


def slack_reason(text: str) -> str:
    return msg('tickets.jira_off') if text == msg('actions.jira_off') else text


def looked_up(read: Callable[[], object], refused: list[str]) -> object:
    try:
        return read()
    except NoAccess as e:
        refused.append(text_of(e))
        return None


def person(jira: object, who: str) -> str | None:
    said: list[str] = []
    found = looked_up(lambda: jira.find_user(who), said)
    if said:
        raise JiraRefused(said[0])
    return found


def scope_shape(settings, screen: dict | None, scope_meta: dict | None, field_kind: Callable[[str], str | None] | None) -> dict | None | bool:
    if screen is not None:
        return scope_meta if scope_meta is not None and scope_meta['kind'] in OPTION_KINDS else False
    found = field_kind(settings.jira_scope_field) if field_kind is not None else None
    return {'kind': found, 'options': None} if found in OPTION_KINDS else None


def create_task(store: Store, jira: object, settings, key: str, title: str | None, by: str, note: str, base_url: str, text: str | None = None, points: float | None = None,
                project: str | None = None, epic: str | None = None, extra_projects: tuple[str, ...] = (), owner_name: str | None = None, *, scope: list[str] | None = None,
                sprint: bool | None = None, fields: dict | None = None, meta: Callable[[str, str], dict | None] | None = None,
                field_kind: Callable[[str], str | None] | None = None) -> tuple[str, str | None, list[str]]:
    if not jira.enabled:
        raise JiraRefused(msg('actions.jira_off'))
    tech = settings.jira_tech_project
    allowed = list(dict.fromkeys(projects_of(settings) + ([tech] if tech else []) + [p for p in extra_projects if PROJECT_KEY.fullmatch(p or '')]))
    project = (project or '').strip().upper() or settings.jira_project
    if project not in allowed:
        raise JiraRefused('project must be one of ' + ', '.join(allowed))
    parent = (epic or '').strip().upper() or (settings.jira_parent if project == settings.jira_project else '')
    if parent and not ticket_pattern(()).fullmatch(parent):
        raise JiraRefused('the epic is a Jira key such as OPS-123')
    if project == tech:
        return create_tech_task(store, jira, settings, key, title, by, note, base_url, text, owner_name) + ([],)
    if points is not None and points not in FIBONACCI:
        raise JiraRefused('story points must be one of 1, 2, 3, 5, 8, 13, 21')
    card = store.anomaly(key)
    summary = (title or '').strip()[:TITLE_LIMIT] or default_title(card, settings.jira_title_prefix)
    issue_type = dict(settings.jira_issue_types).get(project, 'Task')
    screen = meta(project, issue_type) if meta is not None and (scope is not None or fields) else None
    picked = jirafields.texts(scope) if scope is not None else ([settings.jira_dev_scope] if settings.jira_dev_scope else [])
    scope_meta = (screen or {}).get(settings.jira_scope_field) if settings.jira_scope_field else None
    if scope is not None and not picked and scope_meta and scope_meta['required']:
        raise JiraRefused(f"{scope_meta['name']} is required: pick at least one")
    extra = jirafields.given_fields(fields or {}, screen, settings, project, lambda who: person(jira, who)) if fields else {}
    issue = {'project': {'key': project}, 'issuetype': {'name': issue_type}, 'summary': summary,
             'description': typed_description(card, text, card_link(key, base_url)) if (text or '').strip() else description(card, note, card_link(key, base_url))}
    shape = scope_shape(settings, screen, scope_meta, field_kind) if settings.jira_scope_field and picked else None
    sent_scope = picked if shape is not False else []
    if sent_scope:
        issue[settings.jira_scope_field] = jirafields.option_value(shape, scope_meta['name'] if scope_meta else 'scope', sent_scope)
    fields = issue | extra
    if settings.jira_points_field:
        fields[settings.jira_points_field] = settings.jira_points if points is None else points
    if parent:
        fields['parent'] = {'key': parent}
    refused: list[str] = []
    account = known_accounts(getattr(settings, 'jira_accounts', '')).get(by.strip().lower()) or looked_up(lambda: jira.find_user(by), refused)
    users_refused = bool(refused)
    if account:
        fields['assignee'] = {'accountId': account}
        if settings.jira_developer_field:
            fields[settings.jira_developer_field] = {'accountId': account}
    wants_sprint = settings.jira_sprint_default if sprint is None else bool(sprint)
    active = looked_up(lambda: jira.active_sprint(project, settings.jira_sprint_field), refused) if settings.jira_sprint_field and wants_sprint else None
    if active:
        fields[settings.jira_sprint_field] = active
    new_key = jira.create_issue(fields)
    if note:
        store.note(key, note)
    store.triage(key, 'progress', ticket=new_key, owner=by, owner_name=owner_name)
    store.add_activity('jira', by, f'{new_key} created for {key}', key=key)
    log.info('jira task %s created for %s by %s (account %s)', new_key, key, by, account or 'not found')
    missing = None if account or users_refused else f'no Jira account found for {by}: assignee and Developer are empty, fill them in Jira (or map the login in jira.accounts)'
    return new_key, ' \u00b7 '.join([*refused, *([missing] if missing else [])]) or None, sent_scope


def create_tech_task(store: Store, jira: object, settings, key: str, title: str | None, by: str, note: str, base_url: str, text: str | None, owner_name: str | None = None) -> tuple[str, None]:
    card = store.anomaly(key)
    summary = (title or '').strip()[:TITLE_LIMIT] or default_title(card, settings.jira_title_prefix)
    body = typed_description(card, text, card_link(key, base_url)) if (text or '').strip() else description(card, note, card_link(key, base_url))
    fields = {'project': {'key': settings.jira_tech_project}, 'issuetype': {'name': dict(settings.jira_issue_types).get(settings.jira_tech_project, 'Task')}, 'summary': summary, 'description': body}
    if settings.jira_tech_assignee:
        fields['assignee'] = {'accountId': settings.jira_tech_assignee}
    new_key = jira.create_issue(fields)
    if note:
        store.note(key, note)
    store.triage(key, 'progress', ticket=new_key, owner=by, owner_name=owner_name)
    store.add_activity('jira', by, f'{new_key} created for {key}', key=key)
    log.info('jira %s task %s created for %s by %s', settings.jira_tech_project, new_key, key, by)
    return new_key, None
