import logging
import time
from typing import Callable

from skopax.errkind import Authored, kind as error_kind
from skopax.slack import SlackError, reason_of

log = logging.getLogger('skopax.operators')
EMAIL_CACHE_S = 3600


class OperatorUnknown(Authored, Exception):
    pass


class Operators:
    def __init__(self, lookup: Callable[[str], bool] | None, slack, settings, clock=time.time):
        self.lookup = lookup
        self.slack = slack
        self.role = settings.operator_role
        self.admin_role = getattr(settings, 'admin_role', None)
        self.cache_s = settings.slack_operator_cache_s
        self.clock = clock
        self._emails: dict[str, tuple[float, str | None]] = {}
        self._verdicts: dict[str, tuple[float, tuple[str | None, bool]]] = {}

    @property
    def enabled(self) -> bool:
        return self.lookup is not None

    def email_of(self, slack_user: str) -> str | None:
        try:
            return self._email(slack_user)
        except OperatorUnknown:
            return None

    def _email(self, slack_user: str) -> str | None:
        now = self.clock()
        hit = self._emails.get(slack_user)
        if hit and now - hit[0] < EMAIL_CACHE_S:
            return hit[1]
        try:
            email = self.slack.user_email(slack_user)
        except SlackError as e:
            log.warning('users.info for %s failed: %s', slack_user, error_kind(e))
            raise OperatorUnknown(reason_of(e)) from None
        self._emails[slack_user] = (now, email)
        return email

    def access_of(self, email: str) -> tuple[str | None, bool]:
        if self.lookup is None:
            return None, False
        given = email.strip()
        key = given.lower()
        now = self.clock()
        hit = self._verdicts.get(key)
        if hit and now - hit[0] < self.cache_s:
            return hit[1]
        found: tuple[str | None, bool] = (None, False)
        for principal in dict.fromkeys([given, key]):
            try:
                allowed = self.lookup(principal)
            except Exception as e:
                log.warning('role lookup failed: %s', error_kind(e))
                raise OperatorUnknown(error_kind(e)) from None
            if allowed:
                found = (principal, True)
                break
        self._verdicts[key] = (now, found)
        return found

    def principal_for(self, email: str) -> str | None:
        principal, operator = self.access_of(email)
        return principal if operator else None

    def access_for(self, slack_user: str) -> tuple[str | None, bool]:
        if not self.enabled:
            return None, False
        email = self._email(slack_user)
        return self.access_of(email) if email else (None, False)

    def login_for(self, slack_user: str) -> str | None:
        principal, operator = self.access_for(slack_user)
        return principal if operator else None
