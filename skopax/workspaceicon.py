from skopax.ssoicon import SsoIcon

COLLECTION = 'workspace_icon'
PATH = '/api/workspace-icon'
CACHE = 'private, max-age=31536000, immutable'


def store(db) -> SsoIcon:
    return SsoIcon(db, collection=COLLECTION)


def url(version: str | None) -> str | None:
    return f'{PATH}?v={version}' if version else None
