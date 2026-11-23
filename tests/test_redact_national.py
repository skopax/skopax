import time

import pytest

from skopax import redact
from skopax.integrationview import secret_named
from skopax.plugintools import masked
from skopax.redact import REDACTED, national_id_ok, redact_lines, redact_secrets, secrets_masked


def national_id(head: str) -> str:
    d = [int(c) for c in head]
    tenth = ((d[0] + d[2] + d[4] + d[6] + d[8]) * 7 - (d[1] + d[3] + d[5] + d[7])) % 10
    eleventh = (sum(d) + tenth) % 10
    return f'{head}{tenth}{eleventh}'


VALID = national_id('123456789')
OTHER = national_id('987654321')
BROKEN = VALID[:-1] + str((int(VALID[-1]) + 1) % 10)


def test_the_checksum_accepts_a_valid_id_and_rejects_a_changed_digit():
    # then
    assert national_id_ok(VALID) and national_id_ok(OTHER)
    assert not national_id_ok(BROKEN)
    assert not national_id_ok(VALID[:9] + str((int(VALID[9]) + 1) % 10) + VALID[10])
    assert not national_id_ok('0' + VALID[1:])


@pytest.mark.parametrize('line', [
    f'tckn={VALID} login failed',
    f'customer {VALID}',
    f'"nationalId":"{VALID}"',
    f'id:{VALID}.',
    f'ids {VALID},{OTHER}',
])
def test_a_valid_national_id_is_masked(line):
    # when
    out = redact.redact(line)

    # then
    assert VALID not in out and REDACTED in out


@pytest.mark.parametrize('line', [
    f'order {BROKEN} shipped',
    f'resourceVersion: "{BROKEN}"',
    f'order ORD{VALID} shipped',
    f'run_{VALID}',
    f'{VALID}abc',
    f'span 9{VALID} done',
    f'span {VALID}7 done',
    f'price 1.{VALID} TL',
    f'value {VALID}.5',
    'ts 1732350000123 and 1732350000',
    'version 10000000000 at 2026-11-23T10:00:00Z',
])
def test_numbers_that_are_not_a_valid_standalone_national_id_are_kept(line):
    # when
    out = redact.redact(line)

    # then
    assert out == line


def test_valid_looking_eleven_digit_numbers_that_fail_the_checksum_are_kept():
    # given
    numbers = [str(n) for n in range(10_000_000_000, 10_000_000_000 + 3000)]
    failing = [n for n in numbers if not national_id_ok(n)]

    # when
    out = redact_lines([f'order {n}' for n in failing])

    # then
    assert len(failing) > 2900
    assert out == [f'order {n}' for n in failing]


def test_a_national_id_is_masked_in_tool_answers_too():
    # when
    out = redact_lines([f'row {VALID} order 123456789012345'], cards=False)

    # then
    assert out == [f'row {REDACTED} order 123456789012345']


@pytest.mark.parametrize('phone', [
    '0532 123 45 67',
    '05321234567',
    '0532-123-45-67',
    '0532.123.45.67',
    '0 532 123 45 67',
    '0532 123 4567',
    '532 123 45 67',
    '532-123-45-67',
    '532.123.4567',
    '(532) 123 45 67',
    '(0532) 123 45 67',
    '0 (532) 123-45-67',
    '0(532)1234567',
    '0505 123 45 67',
    '0501 123 45 67',
    '501 123 45 67',
    '0544 123 45 67',
    '0555 123 45 67',
    '0561 123 45 67',
    '0532 1234567',
    '0532-1234567',
    '0090 532 123 45 67',
    '90 532 123 45 67',
    '0090 (532) 123 45 67',
])
def test_a_national_format_mobile_number_is_masked(phone):
    # when
    out = redact.redact(f'call {phone} now')

    # then
    assert out == f'call {REDACTED} now'


@pytest.mark.parametrize('line, expected', [
    ('phone:05321234567', f'phone:{REDACTED}'),
    ('tel:0532 123 45 67', f'tel:{REDACTED}'),
    ('alice,05321234567,42', f'alice,{REDACTED},42'),
    ('alice;05321234567;42', f'alice;{REDACTED};42'),
    ('phone=05321234567&x=1', f'phone={REDACTED}&x=1'),
    ('2026-11-23 10:00:00 05321234567 login', f'2026-11-23 10:00:00 {REDACTED} login'),
    ('code 200 0532 123 45 67', f'code 200 {REDACTED}'),
    ('nums 0532 123 45 67 0533 123 45 67', f'nums {REDACTED} {REDACTED}'),
    ('"phone":"0532 123 45 67"', f'"phone":"{REDACTED}"'),
    ('call (0532 123 45 67).', f'call ({REDACTED}).'),
])
def test_a_national_phone_after_a_common_prefix_is_masked(line, expected):
    # when
    out = redact.redact(line)

    # then
    assert out == expected


def test_the_plus_ninety_form_is_still_masked():
    # when
    out = redact.redact('call +90 532 123 45 67 or +905321234567 now')

    # then
    assert out == f'call {REDACTED} or {REDACTED} now'


@pytest.mark.parametrize('line', [
    'size 5368709120 bytes',
    'id 5321234567',
    'cols 512 204 13 27',
    'oid 1.3.6.1.4.1.532.123.45.67',
    'at 2026-05-31 05:32:10.123',
    'date 05.31.2024 and 31.05.2024',
    'version 5.32.123',
    'ip 10.53.123.45 and 192.168.53.12',
    'took 532 ms, 123 45 67 items',
    'amount 532.123,45 TL',
    'run 53212345678901235',
    'list 532 123 45 67 89',
    'x 9532 123 45 67',
    'ref A532-123-45-67',
    'code 0532-123-45-678',
    'line 0212 123 45 67',
    'duration 05:32:12',
    'status 503 120 45 67',
    'status 500 123 4567',
    'status 502 123 4567',
    'got 504 123 45 67 errors',
    'code 503-123-4567',
    'year 90 532',
    'cc 90 5321234567',
    'path /05321234567',
    'v 1.0532 123 45 67',
    'range 1-05321234567',
    'rx 12 540 123 45 67 1000',
    'cols 1 2 532 123 4567 99999',
    'histogram 5 10 532 1234567 2048',
    'seq 100 530 123 4567',
    'pods 3/3 Running 0 532 1234567',
    'rv 553 1234567',
    '1.2.3 532 123 4567',
    'call 532 1234567 now',
    'call 0 532 1234567 now',
])
def test_text_that_only_looks_like_a_national_phone_is_kept(line):
    # when
    out = redact.redact(line)

    # then
    assert out == line


@pytest.mark.parametrize('card', ['5500 0000 0000 0004', '5500-0000-0000-0004', '5500000000000004'])
def test_a_card_that_starts_with_five_is_still_masked_whole_and_kept_in_tool_answers(card):
    # when
    masked = redact.redact(f'card {card} end')
    kept = redact_lines([f'card {card} end'], cards=False)

    # then
    assert masked == f'card {REDACTED} end'
    assert kept == [f'card {card} end']


def test_an_iban_is_still_masked_as_before():
    # when
    out = redact.redact('iban TR33 0006 1005 1978 6457 8413 26 and GB82WEST12345698765432')

    # then
    assert out == f'iban {REDACTED} and {REDACTED}'


@pytest.mark.parametrize('line', [
    'şifre: hunter22x',
    'Şifre=hunter22x',
    'ŞİFRE: hunter22x',
    'SİFRE: hunter22x',
    'SIFRE: hunter22x',
    'ŞIFRE: hunter22x',
    'sifre: hunter22x',
    'şifresi: hunter22x',
    'kullanici_sifre=hunter22x',
    'parola: hunter22x',
    'PAROLASI: hunter22x',
    'şifreniz: hunter22x',
    'Sifreniz=hunter22x',
    'tek kullanımlık şifreniz: hunter22x',
    'şifrem: hunter22x',
    'sifrem=hunter22x',
    'PAROLANIZ: hunter22x',
    'parolaniz: hunter22x',
    'parolam: hunter22x',
    'Passwort: hunter22x',
    'kennwort=hunter22x',
    'contraseña: hunter22x',
    'CONTRASENA=hunter22x',
    'mot de passe : hunter22x',
    'mot_de_passe=hunter22x',
    'motDePasse=hunter22x',
])
def test_secret_key_words_of_every_language_mark_the_value(line):
    # when
    out = secrets_masked(line)

    # then
    assert 'hunter22x' not in out and out.endswith(REDACTED)


def test_a_language_secret_word_names_a_secret_column_and_element():
    # given
    table = '| kullanici | parola |\n| --- | --- |\n| alice | hunter22x |'
    element = '<Kennwort>hunter22x</Kennwort>'

    # when
    out = secrets_masked(table) + secrets_masked(element)

    # then
    assert 'hunter22x' not in out and 'alice' in out


def test_ordinary_words_near_the_secret_words_are_kept():
    # given
    line = 'parolamento: x sifreleme: aes passage: ok motif: blue'

    # when
    out = secrets_masked(line)

    # then
    assert out == line


def test_the_secret_words_come_from_the_language_files(tmp_path):
    # given
    (tmp_path / 'xx').mkdir()
    (tmp_path / 'xx' / 'secret-words.txt').write_text('Geheimwort\nzwei worte\n\nbad*word\n' + 'x' * 41 + '\n', encoding='utf-8')
    (tmp_path / 'yy').mkdir()

    # when
    words = redact.language_secret_words(tmp_path)

    # then
    assert words == ('Geheimwort', 'zwei worte')


def test_a_bad_language_file_is_left_out_and_the_others_are_read(tmp_path, caplog):
    # given
    (tmp_path / 'aa').mkdir()
    (tmp_path / 'aa' / 'secret-words.txt').write_bytes(b'\xff\xfe')
    (tmp_path / 'bb').mkdir()
    (tmp_path / 'bb' / 'secret-words.txt').write_text('geheim\n', encoding='utf-8')

    # when
    with caplog.at_level('WARNING', logger='skopax.redact'):
        words = redact.language_secret_words(tmp_path)

    # then
    assert words == ('geheim',)
    assert 'aa' in caplog.text and 'bb' not in caplog.text


def test_a_language_secret_word_names_a_secret_integration_field_and_tool_key():
    # given
    field = {'key': 'parola', 'label': 'Parola'}
    answer = {'kullanici': 'alice', 'parola': 'hunter22x', 'Kennwort': 'hunter22y'}

    # when
    named = secret_named(field)
    shown = masked(answer, [])

    # then
    assert named and not secret_named({'key': 'kullanici', 'label': 'Kullanici'})
    assert shown == {'kullanici': 'alice', 'parola': REDACTED, 'Kennwort': REDACTED}


def test_a_missing_language_folder_gives_no_words(tmp_path):
    # when
    words = redact.language_secret_words(tmp_path / 'none')

    # then
    assert words == ()


def test_every_built_in_pack_has_secret_words():
    # then
    assert {'şifre', 'sifre', 'parola', 'passwort', 'kennwort', 'contraseña', 'contrasena', 'mot de passe'} <= set(redact.language_secret_words())


def test_long_digit_heavy_lines_are_redacted_in_linear_time():
    # given
    lines = ['5' * 30_000, '532 ' * 7_500, '0 5' * 10_000, f'{VALID} ' * 2_500, '0532 123 45 67, ' * 2_000, '1' * 30_000, '(5' * 15_000]

    # when
    started = time.perf_counter()
    out = [redact.redact(x) for x in lines] + [redact_secrets('şifre ' * 5_000)]
    took = time.perf_counter() - started

    # then
    assert took < 10.0
    assert out[0] == lines[0] and out[5] == lines[5]
    assert VALID not in out[3] and '0532' not in out[4]
