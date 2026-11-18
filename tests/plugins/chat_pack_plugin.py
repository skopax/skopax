from skopax.contract import Locale
from tests.plugins.base import minimal

GERMAN = {'working': 'Arbeite...', 'call': 'Arbeite: {n} Werkzeugaufruf{last}...', 'calls': 'Arbeite: {n} Werkzeugaufrufe{last}...', 'hours': '{n} Std.',
          'starters': (('Was braucht jetzt Aufmerksamkeit?', 'Welche offenen Karten brauchen gerade Aufmerksamkeit, und warum?'),
                       ('Etwas Auffaelliges?', 'Ist in der letzten Stunde etwas auffaellig?')),
          'actions': {'take': 'Uebernehmen', 'resolve': 'Erledigen'}, 'kinds': {'fixed': 'Behoben'}, 'apply': 'Anwenden', 'untitled': 'Unterhaltung', 'none': 'Keine Antwort',
          'refused_cap': '{cap} Nachrichten am Tag sind das Limit.', 'investigator_off': 'Der Ermittler ist aus.', 'login': 'Der Chat braucht eine Anmeldung bei {product}.',
          'refused': 'Das ging nicht.'}

PLUGIN = minimal('chat-pack', 'Chat pack', locales=(Locale('de', chat=GERMAN),))
