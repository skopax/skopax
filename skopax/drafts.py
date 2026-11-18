TECH = 'tech'
INFRA = 'infrastructure'


def is_tech(draft: dict | None) -> bool:
    return bool((draft or {}).get(TECH))


def target(draft: dict | None) -> str:
    draft = draft or {}
    return str(draft.get('project') or '') or (INFRA if is_tech(draft) else '')
