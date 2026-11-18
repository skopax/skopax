from skopax.engine import PassResult, anomaly, parse_iso
from skopax.messages import msg

PASS = 'slack-posts'
PERIOD = 300
OFFSET = 150
WINDOW_S = 3600
LISTED = 8


def failed_posts(cards: list[dict], now: float) -> list[dict]:
    out = []
    for a in cards:
        auto = a.get('autoSend') or {}
        failed, error = auto.get('failedAt'), auto.get('error')
        if a.get('src') == PASS or not failed or not error or now - parse_iso(failed) > WINDOW_S:
            continue
        post = a.get('slackPost') or {}
        if post.get('at') and str(post['at']) >= str(failed):
            continue
        out.append(a)
    return out


def slack_posts_pass(cards: list[dict], now: float) -> list[PassResult]:
    bad = sorted(failed_posts(cards, now), key=lambda a: str(a['autoSend']['failedAt']))
    found = []
    if bad:
        sev = 'crit'
        errors = ', '.join(sorted({str(a['autoSend']['error']) for a in bad}))
        names = ', '.join(f"#{a['num']}" if a.get('num') else str(a['key']) for a in bad[:LISTED]) + (f' and {len(bad) - LISTED} more' if len(bad) > LISTED else '')
        first = str(bad[0]['autoSend']['failedAt'])
        x = anomaly(PASS, 'slack-post-failed', '-', 'skopax-slack', '', sev, len(bad), None, None, '', 'Slack posts failing',
                    msg('cards.slack_posts_failed', count=len(bad), plural='s' if len(bad) != 1 else '', since=first[11:16], errors=errors, names=names),
                    1, f"{sev.upper()} Slack posts failing: {errors}")
        x['ranKey'] = PASS
        found.append(x)
    return [PassResult(PASS, True, 'ok', found, {}, (PASS,))]
