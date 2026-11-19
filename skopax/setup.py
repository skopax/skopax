import hashlib
import html
import re
import secrets
import time
from pathlib import Path

from skopax import brand, clock
from skopax.messages import msg
from skopax.users import clean_email, clean_name, clean_password

CODE_ALPHABET = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789'
CODE_ID = 'setup-code'
PAGE = Path(__file__).parent / 'web' / 'setup.html'
SLOT = re.compile(r'\[\[(\w+)\]\]')
FIELDS = ('title', 'intro', 'code', 'email', 'name', 'password', 'password2', 'submit')


class SetupDone(Exception):
    pass


class BadCode(Exception):
    pass


def new_code() -> str:
    pick = lambda: ''.join(secrets.choice(CODE_ALPHABET) for _ in range(4))
    return f'{pick()}-{pick()}'


def digest(code: str) -> str:
    return hashlib.sha256(str(code or '').strip().upper().encode()).hexdigest()


class SetupGate:
    def __init__(self, db, users, clock=time.time):
        self.meta = db['meta']
        self.users = users
        self.clock = clock
        self.settled = False

    def needed(self) -> bool:
        if self.settled:
            return False
        if self.users.active_admins() == 0:
            return True
        self.settled = True
        return False

    def issue_code(self) -> str | None:
        code = new_code()
        res = self.meta.update_one({'_id': CODE_ID}, {'$setOnInsert': {'hash': digest(code), 'at': clock.stamp(self.clock())}}, upsert=True)
        return code if res.upserted_id is not None else None

    def complete(self, code: str, email: str, name: str, password: str) -> dict:
        email, name, password = clean_email(email), clean_name(name), clean_password(password)
        if not self.needed():
            raise SetupDone()
        taken = self.meta.find_one_and_delete({'_id': CODE_ID, 'hash': digest(code)})
        if taken is None:
            raise BadCode()
        try:
            admin = self.users.create(email, name, 'admin', password, name_from='self')
        except BaseException:
            try:
                self.meta.insert_one(taken)
            except Exception:
                pass
            raise
        self.settled = True
        return admin


def render_form(error: str = '', email: str = '', name: str = '') -> str:
    values = {key: html.escape(msg(f'setup.{key}')) for key in FIELDS}
    values |= {'error': f'<p class="error" role="alert">{html.escape(error)}</p>' if error else '', 'emailvalue': html.escape(email, quote=True), 'namevalue': html.escape(name, quote=True), 'wordmark': brand.wordmark_html(), 'footer': brand.footer_html()}
    page = PAGE.read_text(encoding='utf-8')
    return SLOT.sub(lambda m: values.get(m.group(1), ''), page)
