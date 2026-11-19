from pathlib import Path

from skopax.contract import Capabilities, Check, Context, Field, HttpError, InstanceInfo, Knowledge, PassResult, Plugin, ReadError, RuleSet, Sweep, TestResult, Tool
from skopax.kit.google import GoogleAuth, GoogleError, access_key, bounded, connection_test
from skopax.messages import msg
from skopax.plugins.app_stores.engine import (APPSTORE, APPSTORE_OFF, BUDGET_S, BY, DOT, MAX_DAYS, OPS, PART_NEEDS, PARTS, PLAY, PLAY_OFF, PLAY_REVIEWS, PLAY_SCOPES, RULE_HELP, RULES,
                                              VITALS, AppStore, Play, StoreError, app_id_of, countries_of, gone_keys, links_for, matching, metric_key, package_of, read_play,
                                              read_ratings, run_appstore, run_play, wanted)

ID = 'app-stores'
PER_RUN = 6
TOOL_BUDGET_S = 100
TEST_TIMEOUT_S = 4
TEST_BUDGET_S = 8
NOTHING_SET_UP = 'Set an App Store app id or a Google Play package name to test'
ALL_OFF = 'Nothing to test: the checks of the stores set up here are switched off'
SECTION_HELP = {'appStore': 'App Store rating snapshots per store country: app id, country, app name and the hourly rating count and average of the last 8 days',
                'play': 'Google Play numbers of the last pass: package, the crash and ANR rate of the latest Pacific day and the review counts of the last 24 hours'}
DESCRIPTION = ('App store numbers of the mobile apps, read only. '
               'op ratings (default): the iOS app in one App Store country now (rating count, all-time average, version) and the stored snapshots of the last days with, '
               'per window between two snapshots, how many new ratings came and their average stars (worked out from count and all-time average; Apple gives no review texts). '
               'country picks one of the configured store countries (default the first). '
               f"op vitals: Google Play's user-perceived crash and ANR rates of the Android app per Pacific day for the last days (1-{MAX_DAYS}, default 14), "
               'by day or by version (by: version splits each day per app version); Google treats the app as bad behaviour above 1.09 % crash or 0.47 % ANR. '
               'op reviews: Play reviews with text of the last week as time, stars and app version (texts and names left out). '
               f'Use it on crash, ANR or store-rating cards and after a mobile release. At most {PER_RUN} calls per run.')
SCHEMA = {'type': 'object',
          'properties': {'op': {'type': 'string', 'enum': list(OPS)}, 'days': {'type': 'integer', 'minimum': 1, 'maximum': MAX_DAYS}, 'by': {'type': 'string', 'enum': list(BY)},
                         'country': {'type': 'string', 'maxLength': 2}},
          'additionalProperties': False}
PLAY_RAN = (PLAY, *(metric_key(k) for k in VITALS), PLAY_REVIEWS, access_key(PLAY, 'vitals'), access_key(PLAY, 'reviews'))


def auth_of(ctx: Context, transport) -> GoogleAuth:
    return GoogleAuth(ctx.instance.config.get('credentials') or '', transport, lambda: ctx.now, PLAY_SCOPES)


def not_set_up(store: str) -> str:
    return f"ok{DOT}{msg('app-stores.not_set_up', store=store)}"


def run_appstore_check(ctx: Context) -> Sweep:
    config = ctx.instance.config
    try:
        app_id, countries = app_id_of(config.get('apple-app-id')), countries_of(config.get('countries'))
    except StoreError as e:
        return Sweep(PassResult(APPSTORE, False, f'error: {e.reason}'))
    if not app_id:
        return Sweep(PassResult(APPSTORE, True, not_set_up('App Store'), [], {}, (APPSTORE, *gone_keys(ctx.open_cards(), ()))), sections={'appStore': []})
    stores = [AppStore(app_id, country, ctx.http()) for country in countries]
    return run_appstore(stores, ctx.previous('appStore') or [], ctx.now, ctx.rules('rules'), gone_keys(ctx.open_cards(), countries[1:]), budget=BUDGET_S)


def run_play_check(ctx: Context) -> Sweep:
    try:
        package = package_of(ctx.instance.config.get('play-package'))
    except StoreError as e:
        return Sweep(PassResult(PLAY, False, f'error: {e.reason}'))
    if not package:
        return Sweep(PassResult(PLAY, True, not_set_up('Google Play'), [], {}, PLAY_RAN), sections={'play': []})
    return run_play(Play(auth_of(ctx, bounded(ctx.http(), BUDGET_S)), package), ctx.now, ctx.rules('rules'))


def stores(ctx: Context, args: dict) -> object:
    w = wanted(args)
    config = ctx.instance.config
    app_id, countries, package = app_id_of(config.get('apple-app-id')), countries_of(config.get('countries')), package_of(config.get('play-package'))
    transport = bounded(ctx.http(), TOOL_BUDGET_S)
    if w['op'] == 'ratings':
        if not app_id:
            raise ReadError(400, APPSTORE_OFF)
        country = w['country'] or countries[0]
        if country not in countries:
            raise ReadError(400, f"country must be one of {', '.join(countries)}")
        store = AppStore(app_id, country, transport)
        return read_ratings(store, matching(ctx.previous('appStore') or [], store))
    if not package:
        raise ReadError(400, PLAY_OFF)
    try:
        return read_play(Play(auth_of(ctx, transport), package), w)
    except GoogleError as e:
        raise e.as_refusal(PART_NEEDS['vitals' if w['op'] == 'vitals' else 'reviews']) from None


def test(ctx: Context) -> TestResult:
    config, off = ctx.instance.config, ctx.instance.off_checks

    def ask() -> str:
        app_id, countries, package = app_id_of(config.get('apple-app-id')), countries_of(config.get('countries')), package_of(config.get('play-package'))
        kept = (app_id if APPSTORE not in off else '', package if PLAY not in off else '')
        if (app_id or package) and not any(kept):
            return ALL_OFF
        app_id, package = kept
        parts = []
        if app_id:
            try:
                got = AppStore(app_id, countries[0], ctx.http()).fetch(TEST_TIMEOUT_S)
            except HttpError as e:
                raise StoreError(e.code, f'App Store lookup HTTP {e.code}') from None
            named = ' '.join(x for x in (got['name'] or app_id, got['version']) if x)
            parts.append(f'App Store{DOT}{named} ({countries[0].upper()})')
        if package:
            play = Play(auth_of(ctx, bounded(ctx.http(), TEST_BUDGET_S)), package)
            for part, read in (('vitals', lambda: play.latest_day(VITALS['crash'][0], retry=False)), ('reviews', lambda: play.reviews(limit=1, retry=False))):
                try:
                    read()
                except GoogleError as e:
                    if not e.denied:
                        raise
                    raise e.named(PARTS[part]) from None
            parts.append(f'Google Play{DOT}{package}')
        if not parts:
            raise ReadError(400, NOTHING_SET_UP)
        return DOT.join(parts)

    return connection_test(ask)


def links(card: dict, info: InstanceInfo) -> list[dict] | None:
    try:
        country = countries_of(info.config.get('countries'))[0]
    except StoreError:
        country = 'us'
    return links_for(card, country)


PLUGIN = Plugin(
    id=ID, name='App stores', version='0.1.0', category='apps', description='App Store ratings and Google Play vitals and reviews of the mobile apps.', icon='icon.svg',
    author='Skopax', licence='Apache-2.0', core='>=0.1,<0.3', edition='community',
    capabilities=Capabilities(hosts=('itunes.apple.com', 'playdeveloperreporting.googleapis.com', 'androidpublisher.googleapis.com', 'oauth2.googleapis.com'), secrets=('credentials',)),
    fields=(Field('apple-app-id', 'App Store app id', 'text', help='Numeric App Store id of the iOS app (the digits after "id" in its App Store link). Empty skips the App Store.'),
            Field('countries', 'Store countries', 'text', default='us', help='Comma-separated two-letter App Store countries to read the ratings of, such as us, gb (at most 10).'),
            Field('play-package', 'Google Play package', 'text', help='Package name of the Android app, such as com.example.app. Empty skips Google Play.'),
            Field('credentials', 'Service account key', 'secret',
                  help='The JSON key of a Google service account invited in Play Console with View app information (read-only). Needed for Google Play only.')),
    test=test,
    checks=(Check(APPSTORE, 'App Store ratings', 900, 690, (APPSTORE,), run_appstore_check, tool='stores', sections=('appStore',), section_help={'appStore': SECTION_HELP['appStore']}),
            Check(PLAY, 'Google Play vitals and reviews', 900, 735, (PLAY, PLAY_REVIEWS), run_play_check, tool='stores', sections=('play',),
                  section_help={'play': SECTION_HELP['play']})),
    rules=(RuleSet('rules', 'App store ratings and Play vitals', dict(RULES), RULE_HELP),),
    tools=(Tool('stores', DESCRIPTION, SCHEMA, stores, per_run=PER_RUN, scan=True),),
    links=links,
    knowledge=(Knowledge('app-stores', 'App stores', (Path(__file__).parent / 'app_stores.md').read_text(encoding='utf-8'), slack=True),),
)
