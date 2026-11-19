from collections.abc import Callable
from dataclasses import dataclass

from skopax import brand
from skopax.contract import EDITIONS
from skopax.messages import msg

REFRESH_S = 15
COMMUNITY = EDITIONS[0]


@dataclass(frozen=True)
class Feature:
    id: str
    key: str
    label: str
    help: str
    icon: str
    edition: str = COMMUNITY
    started: str | None = None


FEATURES: tuple[Feature, ...] = (
    Feature('investigator', 'core.investigator', 'AI investigator',
            'Investigates new cards, Investigate clicks, questions from the page and Slack, and deploy-report assessments. Off stops new work at once; queued work waits', 'spark'),
    Feature('chat', 'core.chat', 'Chat', 'The Chat page, Slack direct-message chats and MCP questions', 'chat', started='chat-ui'),
    Feature('slack-autosend', 'core.slack-autosend', 'Slack auto-send',
            'Posts armed Slack drafts and critical cards to the channel on their own; Send to Slack still works while it is off, and armed drafts post once it is on again', 'send'),
    Feature('slack-listener', 'core.slack-listener', 'Slack listener', 'Answers questions, direct messages and button clicks in Slack', 'ear', started='slack-socket'),
    Feature('mcp', 'core.mcp', 'MCP server', 'Lets AI assistants read and triage cards over MCP', 'plug'),
    Feature('public-read', 'access.public-read', 'Read without login', 'Lets anyone who can reach the page read the board without logging in. Changes still need a login', 'eye'),
    Feature('push', 'core.push', 'Phone push', 'Sends new critical cards and task notices to the push users as a Slack direct message', 'phone'),
    Feature('postdeploy', 'core.postdeploy', 'Post-deploy check', 'Checks every prod deploy and writes a deploy report', 'report'),
    Feature('self-review', 'self-review.enabled', 'Self-review', 'Writes the daily self-review of {product} itself and sends it as a Slack direct message', 'check'),
    Feature('update-check', 'core.update-check', 'Check for new versions', 'Asks GitHub once a day for the latest Skopax release and shows an admin notice when a newer one exists', 'tag'),
    Feature('hide-attribution', 'core.hide-attribution', 'Hide "powered by Skopax"', 'Hides the "powered by Skopax" line on every page, in the chat and on Slack and Teams posts', 'badge', edition='enterprise'),
)
KEYS = frozenset(f.key for f in FEATURES)
BY_ID = {f.id: f for f in FEATURES}
BY_KEY = {f.key: f for f in FEATURES}


def off_text(feature_id: str, key: str = 'features.off') -> str:
    return msg(key, feature=BY_ID[feature_id].label)


def needs_edition_text(feature: Feature) -> str:
    return msg('features.needs_edition', feature=feature.label, edition=feature.edition.capitalize())


class Locked(Exception):
    def __init__(self, feature: Feature, edition: str):
        self.detail = needs_edition_text(feature)
        super().__init__(self.detail)
        self.feature = feature
        self.edition = edition


class Features:
    def __init__(self, live, edition: Callable[[], str], needs: Callable[[str], list[str]] = lambda _: [], started: Callable[[str], bool] = lambda _: True):
        self.live = live
        self._edition = edition
        self.needs = needs
        self.started = started
        self._tunables: dict[str, object] = {}

    def edition(self) -> str:
        value = self._edition()
        return value if value in EDITIONS else COMMUNITY

    def locked(self, feature_id: str) -> bool:
        feature = BY_ID[feature_id]
        if feature.edition == COMMUNITY:
            return False
        return EDITIONS.index(feature.edition) > EDITIONS.index(self.edition())

    def _tunable(self, feature: Feature):
        if feature.key not in self._tunables:
            self._tunables[feature.key] = self.live.known()[feature.key]
        return self._tunables[feature.key]

    def stored(self, feature_id: str) -> bool:
        return bool(getattr(self.live.current(), self._tunable(BY_ID[feature_id]).field))

    def on(self, feature_id: str) -> bool:
        return self.stored(feature_id) and not self.locked(feature_id)

    def item(self, feature_id: str) -> dict:
        feature = BY_ID[feature_id]
        stored, locked = self.stored(feature_id), self.locked(feature_id)
        needs = list(self.needs(feature_id))
        restart = msg('features.restart') if feature.started and not needs and not self.started(feature.started) else None
        return {'id': feature.id, 'key': feature.key, 'label': feature.label, 'help': brand.fill(feature.help), 'icon': feature.icon, 'on': stored and not locked, 'stored': stored,
                'locked': locked, 'edition': feature.edition, 'needs': needs, 'restart': restart, 'source': self.live.entry(self._tunable(feature))['source'],
                'reason': needs_edition_text(feature) if locked else None}

    def view(self) -> list[dict]:
        return [self.item(f.id) for f in FEATURES]

    def set(self, feature_id: str, on: bool, by: str) -> dict:
        feature = BY_ID[feature_id]
        if on and self.locked(feature_id):
            raise Locked(feature, self.edition())
        self.live.set(feature.key, 'true' if on else 'false', by)
        return self.item(feature_id)

    def guard(self, key: str, raw) -> None:
        feature = BY_KEY.get(key)
        if feature is not None and str(raw if raw is not None else '').strip() == 'true' and self.locked(feature.id):
            raise Locked(feature, self.edition())
