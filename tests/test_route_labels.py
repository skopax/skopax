import importlib
import re
from pathlib import Path

from skopax.registry import BUILTIN

CARDLOG = Path(__file__).resolve().parents[1] / 'skopax' / 'web' / 'static' / 'cardlog.js'


def test_every_built_in_route_plug_in_has_its_own_posted_words_on_the_card_page():
    # given
    held = re.search(r'^var ROUTE_POSTED = \{(.*)\};$', CARDLOG.read_text(encoding='utf-8'), re.M)
    named = set(re.findall(r"(\w+): \['", held.group(1)))

    # when
    routes = {plugin.id for plugin in (importlib.import_module(name).PLUGIN for name in BUILTIN) if plugin.outbound is not None}

    # then
    assert routes
    assert routes <= named, routes - named
