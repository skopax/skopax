import datetime as dt
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, StrictInt, StringConstraints, field_validator, model_validator

from skopax.config import SETTING_TEXT_MAX
from skopax.history import REF_LIMIT, resolve_problem
from skopax.messages import msg
from skopax.sso import FIELDS as SSO_FIELDS, VALUE_MAX as SSO_VALUE_MAX

CHAT_TEXT_MAX = 8000
Level = Literal['ok', 'info', 'warn', 'crit']
IssueKey = Annotated[str, StringConstraints(pattern=r'^[A-Z][A-Z0-9]{1,9}-\d{1,7}$')]
SignalName = Annotated[str, StringConstraints(pattern=r'^[a-z0-9-]{1,40}$')]
CheckId = Annotated[str, StringConstraints(min_length=1, max_length=64)]
WebLink = Annotated[str, StringConstraints(pattern=r'^https://\S+$', max_length=4000)]
ActionType = Literal['dive', 'ask', 'mute', 'unmute', 'reopen', 'link', 'severity', 'known', 'resolve', 'take', 'group', 'ticket', 'postdeploy', 'assess']
ActionStatus = Literal['requested', 'processing', 'done', 'failed', 'cancelled']


def email_allowed(email: str, domains: tuple[str, ...], closed: bool = False) -> bool:
    if closed:
        return False
    if not domains:
        return True
    return email.rpartition('@')[2].lower() in {d.lower().lstrip('@') for d in domains}


class CodeLink(BaseModel):
    model_config = ConfigDict(extra='allow')

    label: str = Field(min_length=1, max_length=300)
    url: HttpUrl


class Assessment(BaseModel):
    key: str = Field(min_length=1, max_length=500)
    suspects: list[str] | None = Field(default=None, max_length=10)
    recommend: str | None = Field(default=None, max_length=2000)
    code: list[CodeLink] | None = Field(default=None, max_length=20)


class Note(BaseModel):
    key: str = Field(min_length=1, max_length=500)
    text: str = Field(min_length=1, max_length=4000)


class ActionIn(BaseModel):
    type: ActionType
    key: str = Field(min_length=1, max_length=500)
    target: str | None = Field(default=None, max_length=500)
    label: str | None = Field(default=None, max_length=300)
    reason: str | None = Field(default=None, max_length=300)
    hours: float | None = Field(default=None, gt=0, le=24 * 30)
    ticket: str | None = Field(default=None, max_length=100)
    text: str | None = Field(default=None, max_length=CHAT_TEXT_MAX)
    cards: list[str] | None = Field(default=None, max_length=10)
    parent: str | None = Field(default=None, max_length=500)
    title: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    points: float | None = Field(default=None, ge=0, le=100)
    project: str | None = Field(default=None, max_length=20)
    epic: str | None = Field(default=None, max_length=30)
    sev: Literal['crit', 'warn', 'info', 'auto'] | None = None
    reportId: str | None = Field(default=None, max_length=100)
    kind: str | None = Field(default=None, max_length=20)
    ref: str | None = Field(default=None, max_length=REF_LIMIT)

    @model_validator(mode='after')
    def resolve_kind(self):
        problem = resolve_problem(self.kind, self.ref) if self.type == 'resolve' else None
        if problem:
            raise ValueError(problem)
        return self


class SlackPostIn(BaseModel):
    key: str = Field(min_length=1, max_length=500)


class SlackDraftIn(BaseModel):
    key: str = Field(min_length=1, max_length=500)
    text: str = Field(max_length=1500)


class NoticesReadIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    ids: list[Annotated[str, StringConstraints(min_length=1, max_length=64)]] | None = Field(default=None, max_length=200)
    all: bool = False

    @model_validator(mode='after')
    def one_of(self):
        if self.all == (self.ids is not None):
            raise ValueError('send ids or all: true')
        return self


class AssigneeIn(BaseModel):
    key: str = Field(min_length=1, max_length=500)
    name: str | None = Field(default=None, max_length=200)
    email: str | None = Field(default=None, max_length=200)
    me: bool = False


class ChatIn(BaseModel):
    text: str = Field(max_length=CHAT_TEXT_MAX)
    chatId: str | None = Field(default=None, max_length=64)
    cards: list[str] | None = Field(default=None, max_length=3)


class ChatShareIn(BaseModel):
    on: bool


class TagIn(BaseModel):
    key: str = Field(min_length=1, max_length=500)
    tag: str = Field(min_length=1, max_length=30)
    on: bool


class PasswordIn(BaseModel):
    current: str = Field(max_length=256)
    new: str = Field(max_length=256)


class NameIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(max_length=400)


class NoticeMailIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    on: bool
    kinds: list[Annotated[str, StringConstraints(min_length=1, max_length=40)]] = Field(default_factory=list, max_length=64)


class AdminSettingIn(BaseModel):
    key: str = Field(min_length=1, max_length=120)
    value: str = Field(max_length=SETTING_TEXT_MAX)


class TeamsRouteIn(BaseModel):
    name: str = Field(min_length=1, max_length=30)
    webhook: str | None = Field(default=None, max_length=2000)
    match: str | None = Field(default=None, max_length=2000)
    minSev: str | None = Field(default=None, max_length=10)
    mention: str | None = Field(default=None, max_length=2000)


class BusinessIn(BaseModel):
    key: str = Field(min_length=1, max_length=500)
    text: str = Field(max_length=1000)
    why: str | None = Field(default=None, max_length=600)
    kind: Literal['action', 'info'] | None = None


class SlackHoldIn(BaseModel):
    key: str = Field(min_length=1, max_length=500)


class ReportSlackDraftIn(BaseModel):
    text: str = Field(max_length=1500)


class SlackUpdateIn(BaseModel):
    key: str = Field(min_length=1, max_length=500)
    text: str = Field(max_length=1000)


class RouteSendIn(BaseModel):
    key: str = Field(min_length=1, max_length=500)
    route: str | None = Field(default=None, max_length=100)


class DeployRow(BaseModel):
    ns: str | None = Field(default=None, max_length=100)
    svc: str = Field(min_length=1, max_length=200)
    fromImage: str | None = Field(default=None, max_length=100)
    toImage: str | None = Field(default=None, max_length=100)
    at: str | None = Field(default=None, max_length=40)
    flags: list[str] = Field(default_factory=list, max_length=20)
    metrics: str | None = Field(default=None, max_length=1000)
    note: str | None = Field(default=None, max_length=1000)
    issues: list[IssueKey] = Field(default_factory=list, max_length=100)
    issuesNote: str | None = Field(default=None, max_length=200)
    cluster: str | None = Field(default=None, max_length=100)
    links: dict[SignalName, WebLink] | None = Field(default=None, max_length=20)


class ReleasedIssue(BaseModel):
    key: IssueKey
    summary: str | None = Field(default=None, max_length=300)
    status: str | None = Field(default=None, max_length=60)
    inRelease: bool | None = None


class ReportBrief(BaseModel):
    level: Level = 'info'
    headline: str = Field(min_length=1, max_length=500)
    points: list[Annotated[str, StringConstraints(max_length=500)]] = Field(default_factory=list, max_length=10)


class ReportBriefIn(ReportBrief):
    level: Level | None = None


class DeployReportIn(BaseModel):
    summary: str = Field(min_length=1, max_length=1000)
    brief: ReportBrief | None = None
    windowFrom: str | None = Field(default=None, max_length=40)
    windowTo: str | None = Field(default=None, max_length=40)
    rows: list[DeployRow] = Field(default_factory=list, max_length=200)
    releases: list[str] = Field(default_factory=list, max_length=10)
    issues: list[ReleasedIssue] = Field(default_factory=list, max_length=300)
    text: str | None = Field(default=None, max_length=20000)
    actionId: str | None = Field(default=None, max_length=100)
    replaces: list[Annotated[str, StringConstraints(max_length=100)]] = Field(default_factory=list, max_length=10)


class ImageBy(BaseModel):
    names: list[Annotated[str, StringConstraints(max_length=100)]] = Field(default_factory=list, max_length=10)
    title: str | None = Field(default=None, max_length=300)
    date: str | None = Field(default=None, max_length=40)
    sha: str | None = Field(default=None, max_length=40)
    url: HttpUrl | None = None
    commits: int | None = Field(default=None, ge=0)
    compare: HttpUrl | None = None


class DeployIn(BaseModel):
    ns: str = Field(min_length=1, max_length=100)
    svc: str = Field(min_length=1, max_length=200)
    fromImage: str | None = Field(default=None, max_length=100)
    toImage: str = Field(min_length=1, max_length=100)
    at: str = Field(max_length=40)
    imageBy: ImageBy | None = None
    tickets: list[IssueKey] = Field(default_factory=list, max_length=100)
    ticketsNote: str | None = Field(default=None, max_length=200)
    releases: list[Annotated[str, StringConstraints(min_length=1, max_length=100)]] | None = Field(default=None, max_length=20)

    @field_validator('at')
    @classmethod
    def utc_iso(cls, v: str) -> str:
        try:
            t = dt.datetime.fromisoformat(v.replace('Z', '+00:00'))
        except ValueError:
            raise ValueError('at must be an ISO time') from None
        if t.tzinfo is None:
            raise ValueError('at needs a time zone')
        return t.astimezone(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


class ActionPatch(BaseModel):
    status: ActionStatus
    result: str | None = Field(default=None, max_length=1000)
    report: str | None = Field(default=None, max_length=20000)
    expect: ActionStatus | None = None


class InvestigateIn(BaseModel):
    stopped: bool


class SelfReviewIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8000)]
    dm: Annotated[list[Annotated[str, StringConstraints(strip_whitespace=True, pattern=r'^[^@\s]+@[^@\s]+$')]], Field(max_length=5)] = []


class PostdeployIgnoreIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    services: Annotated[list[Annotated[str, StringConstraints(strip_whitespace=True, pattern=r'^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$')]], Field(max_length=50)]


class GateExamplesIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    examples: Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)]


class SmallTalkIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool = Field(strict=True)
    rules: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
    hourlyCap: int = Field(strict=True, ge=0, le=60)


class UserIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    email: str = Field(max_length=320)
    name: str = Field(default='', max_length=400)
    role: str = Field(max_length=20)
    password: str | None = Field(default=None, max_length=1000)
    sso: bool = Field(default=False, strict=True)


class UserPatch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    role: str | None = Field(default=None, max_length=20)
    disabled: bool | None = Field(default=None, strict=True)
    password: str | None = Field(default=None, max_length=1000)
    ssoLink: bool | None = Field(default=None, strict=True)
    name: str | None = Field(default=None, max_length=400)
    ssoOnly: bool | None = Field(default=None, strict=True)


class TokenIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str
    role: str = Field(max_length=20)
    expiresDays: int | None = Field(default=None, ge=1, le=3650, strict=True)


class PluginSwitchIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool = Field(strict=True)


class TagColorIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    color: StrictInt | None = Field(ge=0, le=9)


class TagAutoIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    on: bool


class TagOwnersIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    emails: list[Annotated[str, StringConstraints(max_length=320)]] = Field(max_length=100)


class FeatureIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    on: bool = Field(strict=True)


class AiKeyIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    key: str = Field(max_length=400)


class AiKeyTestIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    key: str | None = Field(default=None, max_length=400)


class InstanceIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(max_length=20)
    values: dict[str, str | None] = Field(default_factory=dict, max_length=64)
    enabled: bool = Field(default=False, strict=True)
    offChecks: list[CheckId] = Field(default_factory=list, max_length=64)


class InstancePatch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    values: dict[str, str | None] | None = Field(default=None, max_length=64)
    enabled: bool | None = Field(default=None, strict=True)
    primary: Literal[True] | None = None
    offChecks: list[CheckId] | None = Field(default=None, max_length=64)


class PluginTestIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str | None = Field(default=None, max_length=20)
    values: dict[str, str | None] = Field(default_factory=dict, max_length=64)
    offChecks: list[CheckId] | None = Field(default=None, max_length=64)


class TestMailIn(BaseModel):
    model_config = ConfigDict(extra='forbid')


class FieldSuggestIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str | None = Field(default=None, max_length=20)
    values: dict[str, str | None] = Field(default_factory=dict, max_length=64)


SsoValue = Annotated[str, StringConstraints(max_length=SSO_VALUE_MAX)]


def known_sso_fields(values: dict[str, str | None]) -> dict[str, str | None]:
    unknown = sorted(set(values) - set(SSO_FIELDS))
    if unknown:
        raise ValueError(msg('sso.unknown_field', fields=', '.join(name[:60] for name in unknown[:5])))
    return values


class SsoIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    values: dict[str, SsoValue | None] = Field(default_factory=dict, max_length=len(SSO_FIELDS))
    enabled: bool | None = Field(default=None, strict=True)

    _known = field_validator('values')(known_sso_fields)


class SsoTestIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    values: dict[str, SsoValue | None] = Field(default_factory=dict, max_length=len(SSO_FIELDS))

    _known = field_validator('values')(known_sso_fields)
