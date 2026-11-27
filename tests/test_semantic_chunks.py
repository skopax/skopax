import datetime as dt
import json

import pytest

from skopax.contract import Knowledge
from skopax.redact import REDACTED, TIMEOUT_MARK
from skopax.semantic import chunks
from skopax.semantic.chunks import Tally

TR = ''.join(chr(c) for c in (0x15f, 0x11f, 0x131, 0x130, 0xe7, 0xf6, 0xfc))
DE = ''.join(chr(c) for c in (0xe4, 0xf6, 0xfc, 0xdf))
FR = ''.join(chr(c) for c in (0xe9, 0xe8, 0xe0, 0xe7, 0x153))
SECRET = 'AKIAIOSFODNN7EXAMPLE'
CARD = '4111 1111 1111 1111'
GUARD_TEXTS = ['GUARDFIRST-zq1', 'GUARDFIX-zq2', 'GUARDREASON-zq3', 'HELDTEXT-zq4', 'TOOLINPUT-zq5', 'HELDWRITE-zq6', 'SHOT-zq7', 'ACTIVITY-zq8']


def run_doc(**over):
    doc = {'_id': 'r1', 'outcome': 'done', 'trigger': 'cards', 'keys': ['cpu|shop/checkout-service', 'b', 'c', 'd', 'e'], 'startedAt': '2026-11-01T10:00:00Z',
           'ask': {'text': 'Why is checkout slow?'}, 'recap': 'Pool exhausted after deploy.',
           'guard': {'first': GUARD_TEXTS[0], 'fix': GUARD_TEXTS[1], 'reason': GUARD_TEXTS[2]},
           'held': {'text': GUARD_TEXTS[3], 'writes': [GUARD_TEXTS[5]], 'proposed': [GUARD_TEXTS[5]]},
           'toolCalls': [{'name': 'x', 'input': {'q': GUARD_TEXTS[4]}, 'output': GUARD_TEXTS[4]}], 'screenshots': [GUARD_TEXTS[6]], 'activity': [GUARD_TEXTS[7]]}
    return doc | over


def card_doc(**over):
    doc = {'_id': 'k1|2026', 'key': 'cpu|shop/checkout-service', 'svc': 'checkout-service', 'label': 'High CPU', 'sub': 'pod-a', 'sev': 'warn', 'what': 'CPU above 90 percent',
           'outcome': 'fixed', 'resolution': {'kind': 'fixed', 'by': 'alice'}, 'note': 'Pool leak in v2.\nRolled back.', 'muted': 'known batch job', 'ticket': {'key': 'OPS-123'},
           'guard': {'first': GUARD_TEXTS[0]}, 'held': {'text': GUARD_TEXTS[3]}, 'asks': [{'answer': GUARD_TEXTS[4]}]}
    return doc | over


def everything(passages):
    return '\n'.join(p.text for p in passages)


def test_run_builds_header_ask_and_recap():
    # given
    doc = run_doc()
    # when
    out = chunks.run_passages(doc)
    # then
    assert len(out) == 1
    p = out[0]
    assert p.kind == 'run' and p.ref == 'r1' and p.part == 0
    assert p.text.startswith('cards: cpu|shop/checkout-service, b, c, d svc: checkout-service\n')
    assert 'Why is checkout slow?' in p.text and 'Pool exhausted after deploy.' in p.text
    assert p.f == {'svc': 'checkout-service', 'keys': ['cpu|shop/checkout-service', 'b', 'c', 'd'], 'trigger': 'cards', 'started': '2026-11-01T10:00:00Z'}


@pytest.mark.parametrize('over', [{'outcome': 'held'}, {'outcome': 'releasing'}, {'outcome': 'dropped'}, {'outcome': 'discarded'}, {'outcome': None}, {'trigger': 'chat'}, {'trigger': None}])
def test_run_not_done_or_chat_gives_nothing(over):
    # given
    doc = run_doc(**over)
    # when
    out = chunks.run_passages(doc)
    # then
    assert out == []


def test_forbidden_fields_never_reach_any_passage():
    # given
    run, card = run_doc(), card_doc()
    open_card = card_doc(status='open', muted=None, what='x')
    deploy = {'_id': 'd1', 'ns': 'shop', 'svc': 'checkout-service', 'fromImage': 'a:1', 'toImage': 'a:2', 'imageBy': 'Fix pool', 'tickets': ['OPS-123'], 'guard': GUARD_TEXTS[0], 'held': GUARD_TEXTS[3]}
    report = {'_id': 'p1', 'summary': '2 deploys', 'brief': {'headline': 'Errors up', 'points': ['p1']}, 'guard': GUARD_TEXTS[0], 'rows': [GUARD_TEXTS[7]]}
    # when
    out = chunks.run_passages(run) + chunks.card_passages(card) + chunks.open_passages(open_card) + chunks.deploy_passages(deploy) + chunks.report_passages(report)
    # then
    assert len(out) == 5
    blob = everything(out) + json.dumps([p.f for p in out])
    for text in GUARD_TEXTS:
        assert text not in blob


def test_card_builder_uses_allow_listed_fields():
    # given
    doc = card_doc()
    # when
    out = chunks.card_passages(doc)
    # then
    assert len(out) == 1
    text = out[0].text
    assert text.splitlines()[0] == 'card cpu|shop/checkout-service svc: checkout-service label: High CPU sub: pod-a'
    for part in ('what: CPU above 90 percent', 'outcome: ', 'cause: Pool leak in v2.', 'Rolled back.', 'verdict: known batch job', 'ticket: OPS-123'):
        assert part in text
    assert out[0].f == {'svc': 'checkout-service', 'label': 'High CPU', 'sev': 'warn'}
    assert GUARD_TEXTS[0] not in text and GUARD_TEXTS[4] not in text


def test_open_builder_skips_muted_and_non_open():
    # given
    live = {'_id': 'cpu|shop/x', 'key': 'cpu|shop/x', 'svc': 'x', 'label': 'High CPU', 'what': 'CPU high', 'note': {'text': 'looking'}, 'status': 'open'}
    # when
    out = chunks.open_passages(live)
    muted = chunks.open_passages(live | {'muted': 'noise'})
    cleared = chunks.open_passages(live | {'status': 'cleared'})
    # then
    assert [p.ref for p in out] == ['cpu|shop/x'] and 'what: CPU high' in out[0].text and 'note: looking' in out[0].text
    assert muted == [] and cleared == []


def test_change_builders():
    # given
    deploy = {'_id': 'd1', 'ns': 'shop', 'svc': 'checkout-service', 'fromImage': 'reg/app:1', 'toImage': 'reg/app:2', 'imageBy': 'Fix pool leak', 'tickets': [{'key': 'OPS-123'}, 'OPS-124']}
    report = {'_id': 'p1', 'summary': '2 deploys: 1 flagged', 'brief': {'headline': 'Errors up', 'points': ['checkout-service: errors', 'other']}}
    # when
    d, r = chunks.deploy_passages(deploy)[0], chunks.report_passages(report)[0]
    # then
    assert d.kind == 'change' and d.ref == 'deploy:d1' and d.text.startswith('deploy shop/checkout-service\n')
    assert 'reg/app:1 -> reg/app:2' in d.text and 'by: Fix pool leak' in d.text and 'OPS-123, OPS-124' in d.text
    assert r.ref == 'report:p1' and 'Errors up' in r.text and 'checkout-service: errors' in r.text and '2 deploys: 1 flagged' in r.text


def test_knowledge_doc_and_errtpl_builders():
    # given
    note = Knowledge(id='runbook', title='Runbook', text='# Intro\nrestart the pod', slack=True)
    # when
    k = chunks.knowledge_passages('wiki', note, instance='main')[0]
    d = chunks.doc_passages('page:1', 'Checkout page', 'body text', plugin='confluence', instance='main', space='OPS')[0]
    e = chunks.errtpl_passages('t1', 'checkout-service', 'error', 'timeout calling <id>', plugin='loki', instance='main')[0]
    # then
    assert k.text.startswith('wiki: Runbook\n') and k.ref == 'wiki:runbook' and k.f == {'plugin': 'wiki', 'instance': 'main', 'slack': True}
    assert d.text == 'Checkout page\nbody text' and d.f == {'plugin': 'confluence', 'instance': 'main', 'space': 'OPS'}
    assert e.text == 'svc: checkout-service level: error\ntimeout calling <id>' and e.f['level'] == 'error'


def test_secrets_emails_and_card_numbers_masked():
    # given
    doc = run_doc(ask={'text': f'key {SECRET} mail alice@example.com card {CARD}'}, recap='password=hunter2hunter2 done')
    # when
    out = chunks.run_passages(doc)
    # then
    text = everything(out)
    assert out and SECRET not in text and 'alice@example.com' not in text and CARD not in text and 'hunter2hunter2' not in text
    assert REDACTED in text


def test_fully_redacted_body_is_skipped_and_counted():
    # given
    tally = Tally()
    doc = run_doc(ask={'text': SECRET}, recap='')
    # when
    out = chunks.run_passages(doc, tally)
    # then
    assert out == [] and tally.skipped == 1


def test_timeout_mark_skips_the_chunk_and_counts(monkeypatch):
    # given
    real = chunks.redact_text

    def flaky(text, limit=None):
        return TIMEOUT_MARK if 'SLOWBIT' in text and len(text) < 1400 else real(text, limit)

    monkeypatch.setattr(chunks, 'redact_text', flaky)
    first, second, third = 'alpha ' * 150, 'SLOWBIT ' + 'beta ' * 150, 'gamma ' * 150
    tally = Tally()
    # when
    out = chunks.doc_passages('p', 'T', '\n\n'.join([first, second, third]), tally=tally)
    # then
    assert tally.skipped >= 1 and out
    assert all('SLOWBIT' not in p.text for p in out)
    assert all(TIMEOUT_MARK not in p.text for p in out)


def test_timeout_on_whole_body_skips_everything(monkeypatch):
    # given
    monkeypatch.setattr(chunks, 'redact_text', lambda text, limit=None: TIMEOUT_MARK)
    tally = Tally()
    # when
    out = chunks.doc_passages('p', 'T', 'some text', tally=tally)
    # then
    assert out == [] and tally.skipped == 1


def test_short_text_is_one_chunk():
    # given
    text = 'short'
    # when
    out = chunks.split(text)
    # then
    assert out == ['short']


def test_window_and_overlap_boundaries():
    # given
    words = [f'w{i:04d}' for i in range(900)]
    text = ' '.join(words)
    # when
    out = chunks.split(text)
    # then
    assert len(out) > 2
    assert all(len(c) <= chunks.WINDOW for c in out)
    for a, b in zip(out, out[1:]):
        shared = next(n for n in range(min(len(a), len(b)), 0, -1) if a.endswith(b[:n]))
        assert 0 < shared <= chunks.OVERLAP
        assert shared >= chunks.OVERLAP - 12
    assert out[0].startswith('w0000') and out[-1].endswith('w0899')


def test_split_prefers_headings_then_paragraphs():
    # given
    first = '# One\n' + ('a' * 40 + '. ') * 20
    second = '# Two\n' + ('b' * 40 + '. ') * 20
    third = '# Three\n' + 'c ' * 100
    text = first + '\n' + second + '\n' + third
    # when
    out = chunks.split(text, window=1000, overlap=0)
    # then
    assert out[0].startswith('# One') and any(c.startswith('# Two') or c.startswith('# Three') for c in out[1:])
    assert all(len(c) <= 1000 for c in out)


def test_long_sentence_without_breaks_is_hard_cut():
    # given
    text = 'x' * 3000
    # when
    out = chunks.split(text)
    # then
    assert all(len(c) <= chunks.WINDOW for c in out) and len(out) >= 3


def test_header_on_every_chunk_and_caps():
    # given
    text = '\n\n'.join(f'para {i} ' + 'word ' * 150 for i in range(60))
    # when
    out = chunks.doc_passages('p', 'My title', text)
    notes = chunks.knowledge_passages('wiki', Knowledge(id='n', title='Big', text=text))
    run = chunks.run_passages(run_doc(recap=text))
    # then
    assert len(out) == 20 and len(notes) == 20 and len(run) == 3
    assert [p.part for p in out] == list(range(20))
    assert all(p.text.startswith('My title\n') for p in out)
    assert all(p.text.startswith('wiki: Big\n') for p in notes)
    assert all(p.text.startswith('cards: ') for p in run)


@pytest.mark.parametrize('builder', ['card', 'open', 'change', 'errtpl'])
def test_single_part_kinds_are_capped_at_one(builder):
    # given
    big = 'word ' * 2000
    # when
    out = {'card': lambda: chunks.card_passages(card_doc(note=big)), 'open': lambda: chunks.open_passages({'_id': 'k', 'what': big}),
           'change': lambda: chunks.report_passages({'_id': 'p', 'summary': big}), 'errtpl': lambda: chunks.errtpl_passages('t', 's', 'error', big)}[builder]()
    # then
    assert len(out) == 1


def test_non_ascii_letters_survive():
    # given
    text = f'{TR} {DE} {FR}'
    # when
    out = chunks.doc_passages('p', f'T{TR}', text)
    # then
    assert out[0].text == f'T{TR}\n{text}'
    assert chunks.split(text) == [text]


def test_hash_is_stable_and_covers_text_with_header():
    # given
    doc = run_doc()
    # when
    a, b = chunks.run_passages(doc)[0], chunks.run_passages(doc)[0]
    other = chunks.run_passages(run_doc(recap='Different recap.'))[0]
    # then
    assert a.hash == b.hash and a.hash != other.hash and len(a.hash) == 64
    assert a.hash == chunks.text_hash(a.text)


def test_fingerprint_changes_with_model_dims_prefix_and_version(monkeypatch):
    # given
    base = chunks.fingerprint('m', 384, 'passage: ')
    # when
    same = chunks.fingerprint('m', 384, 'passage: ')
    other = [chunks.fingerprint('n', 384, 'passage: '), chunks.fingerprint('m', 768, 'passage: '), chunks.fingerprint('m', 384, '')]
    monkeypatch.setattr(chunks, 'CHUNKER_VERSION', chunks.CHUNKER_VERSION + 1)
    bumped = chunks.fingerprint('m', 384, 'passage: ')
    # then
    assert same == base
    assert len({base, bumped, *other}) == 5


def test_secret_across_chunk_boundary_is_masked_before_splitting():
    # given
    filler = 'word ' * 235
    text = filler + f'token {SECRET} ' + 'tail ' * 50
    # when
    out = chunks.doc_passages('p', 'T', text)
    # then
    assert SECRET not in everything(out) and SECRET[:10] not in everything(out)


def test_markdown_splits_on_headings_end_to_end():
    # given
    one = '# One\n\n' + ('alpha beta gamma. ' * 40)
    two = '# Two\n\n' + ('delta epsilon zeta. ' * 35)
    three = '# Three\n\n' + ('eta theta iota. ' * 40)
    text = f'{one}\n\n{two}\n\n{three}'

    # when
    out = chunks.doc_passages('p', 'Guide', text)

    # then
    assert len(out) == 3
    assert 'delta' not in out[0].text and 'eta theta' not in out[0].text
    assert out[0].text.startswith('Guide\n# One')
    assert '# Two' in out[1].text and 'eta theta' not in out[1].text.split('# Two')[1]
    assert '# Three' in out[2].text


def test_blank_lines_split_paragraphs_end_to_end():
    # given
    paras = [f'{name} ' + ('word ' * 200) for name in ('PARA-A', 'PARA-B', 'PARA-C')]
    text = '\n\n'.join(paras)

    # when
    out = chunks.doc_passages('p', 'T', text)

    # then
    assert len(out) == 3
    assert 'PARA-B' not in out[0].text and 'PARA-A' not in out[1].text and 'PARA-C' not in out[1].text and 'PARA-C' in out[2].text


def test_a_card_number_straddling_a_chunk_boundary_never_shows_a_fragment():
    # given
    text = 'word ' * 238 + CARD + ' ' + 'tail ' * 80
    assert '4111 1111' in chunks.split(text)[0] and CARD not in chunks.split(text)[0]

    # when
    out = chunks.doc_passages('p', 'T', text)

    # then
    assert len(out) == 2
    assert '4111' not in everything(out) and '1111' not in everything(out)


def test_a_secret_with_a_timeout_in_the_whole_body_pass_skips_everything(monkeypatch):
    # given
    real = chunks.redact_text
    monkeypatch.setattr(chunks, 'redact_text', lambda text, limit=None: TIMEOUT_MARK if len(text) > 2000 else real(text, limit))
    tally = Tally()

    # when
    out = chunks.doc_passages('p', 'T', 'word ' * 600, tally=tally)

    # then
    assert out == [] and tally.skipped == 1


def test_header_is_capped_and_masked_but_card_keys_are_kept():
    # given
    keys = ['cpu|shop/checkout-service', 'b']
    doc = run_doc(keys=keys, svc=f'checkout-service {SECRET} ' + 'x' * 400)

    # when
    out = chunks.run_passages(doc)

    # then
    head = out[0].text.split('\n')[0]
    assert len(head) <= 200 and SECRET not in head
    assert head.startswith('cards: cpu|shop/checkout-service, b')


def test_card_header_keeps_the_key_and_masks_the_rest():
    # given
    doc = card_doc(label=f'High CPU {SECRET}', sub='alice@example.com')

    # when
    head = chunks.card_passages(doc)[0].text.split('\n')[0]

    # then
    assert head.startswith('card cpu|shop/checkout-service svc: checkout-service label: High CPU')
    assert SECRET not in head and 'alice@example.com' not in head


def test_filter_values_are_masked_and_capped():
    # given
    doc = run_doc(svc=f'shop {SECRET} ' + 'y' * 300, keys=['a|b/c', f'k {SECRET}', 'c', 'd', 'e'], trigger='alice@example.com')

    # when
    f = chunks.run_passages(doc)[0].f

    # then
    assert len(f['svc']) <= 120 and SECRET not in json.dumps(f) and 'alice@example.com' not in json.dumps(f)
    assert len(f['keys']) == 4 and all(len(k) <= 120 for k in f['keys'])


def test_filter_values_with_a_timeout_are_dropped_to_empty(monkeypatch):
    # given
    real = chunks.redact_text
    monkeypatch.setattr(chunks, 'redact_text', lambda text, limit=None: TIMEOUT_MARK if text == 'SLOWF' else real(text, limit))

    # when
    out = chunks.doc_passages('p', 'T', 'body', plugin='SLOWF', space='OPS')

    # then
    assert out[0].f['plugin'] == '' and out[0].f['space'] == 'OPS'


def test_fingerprint_without_dims_returns_none_until_the_model_answered():
    # given
    model = 'm'

    # when
    unknown = chunks.fingerprint(model, None, 'passage: ')

    # then
    assert unknown is None
    assert chunks.fingerprint('m', 384, 'passage: ') is not None


def test_passages_carry_the_source_time():
    # given
    stamp = dt.datetime(2026, 11, 2, 8, 30, tzinfo=dt.timezone.utc)
    card = card_doc(lastSeen='2026-11-03T09:00:00Z')
    deploy = {'_id': 'd1', 'ns': 'shop', 'svc': 'x', 'fromImage': 'a:1', 'toImage': 'a:2', 'at': '2026-11-04T10:00:00Z'}

    # when
    run = chunks.run_passages(run_doc())[0]
    card_p = chunks.card_passages(card)[0]
    deploy_p = chunks.deploy_passages(deploy)[0]
    page = chunks.doc_passages('p', 'T', 'body', modified=stamp)[0]
    unknown = chunks.doc_passages('p', 'T', 'body')[0]
    note = chunks.knowledge_passages('wiki', Knowledge(id='n', title='N', text='t'), seen='2026-11-05T00:00:00Z')[0]
    fresh = chunks.errtpl_passages('t', 's', 'error', 'tpl')[0]

    # then
    assert run.at == dt.datetime(2026, 11, 1, 10, 0, tzinfo=dt.timezone.utc)
    assert card_p.at == dt.datetime(2026, 11, 3, 9, 0, tzinfo=dt.timezone.utc)
    assert deploy_p.at == dt.datetime(2026, 11, 4, 10, 0, tzinfo=dt.timezone.utc)
    assert page.at == stamp and unknown.at is None
    assert note.at == dt.datetime(2026, 11, 5, tzinfo=dt.timezone.utc)
    assert fresh.at is not None and fresh.at.tzinfo is not None


def test_a_short_key_inside_a_secret_does_not_defeat_masking():
    # given
    header = f'svc: {SECRET}'

    # when
    direct = chunks.masked_header(header, ('A',))
    through_run = chunks.run_passages(run_doc(keys=['A', 'K'], svc=SECRET))

    # then
    assert direct is not None and SECRET not in direct and 'AKIA' not in direct
    assert through_run and all(SECRET not in p.text and 'AKIA' not in p.text.split('\n')[0] for p in through_run)


def test_a_key_that_looks_like_a_placeholder_is_not_mangled():
    # given
    keys = ['zzkeepslot0zz', 'cpu|shop/checkout-service']

    # when
    head = chunks.masked_header('cards: zzkeepslot0zz, cpu|shop/checkout-service svc: x', tuple(keys))

    # then
    assert head == 'cards: zzkeepslot0zz, cpu|shop/checkout-service svc: x'


def test_a_real_card_key_stays_intact_and_the_rest_is_masked():
    # given
    key = 'cpu|shop/checkout-service'

    # when
    head = chunks.masked_header(f'card {key} svc: x {SECRET}', (key,))

    # then
    assert head.startswith(f'card {key} svc: x') and SECRET not in head


def test_a_non_card_key_is_masked_like_any_other_text():
    # given
    key = 'password=hunter2hunter2'

    # when
    head = chunks.masked_header(f'cards: {key}', (key,))

    # then
    assert 'hunter2hunter2' not in head


def test_the_full_header_with_kept_keys_still_caps_at_200():
    # given
    key = 'cpu|shop/checkout-service'
    header = f'cards: {key}, ' + 'x' * 400

    # when
    head = chunks.masked_header(header, (key,))

    # then
    assert len(head) <= 200 and head.startswith(f'cards: {key}')
