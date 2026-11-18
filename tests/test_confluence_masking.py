import json

import pytest

from skopax.plugins.confluence.client import masked, plain
from skopax.redact import secrets_masked
from skopax.store import Store
from tests.test_confluence_tool import PAGE_CONTENT, call, fake, fresh_cloud_ids, wiki_box


def page_text(markup: str) -> str:
    return masked(plain(markup))


def code(body: str) -> str:
    return f'<ac:structured-macro ac:name="code"><ac:plain-text-body><![CDATA[{body}]]></ac:plain-text-body></ac:structured-macro>'


def test_a_quoted_or_camel_case_key_in_a_config_block_is_masked():
    # given
    markup = (code('{\n  "spring.datasource.password": "Pr0d-S3cret!",\n  "dbPassword": "Camel-Pa55",\n  "api_key": "k\\"ey-Tail9"\n}')
              + code("config = {'password': 'Py-Pa55'}\naccessToken: Tok-77abc"))

    # when
    text = page_text(markup)

    # then
    assert '"spring.datasource.password": [redacted],' in text and '"dbPassword": [redacted],' in text and '"api_key": [redacted]' in text
    assert "{'password': [redacted]}" in text and 'accessToken: [redacted]' in text
    for leaked in ('Pr0d-S3cret!', 'Camel-Pa55', 'Tail9', 'Py-Pa55', 'Tok-77abc'):
        assert leaked not in text


def test_a_secret_xml_element_keeps_its_tags_and_loses_its_value():
    # given
    markup = (code('<server>\n  <id>nexus</id>\n  <username>deploy</username>\n  <password>Nexus-Pa55</password>\n  <keyStorePassword>Ks-Pa55</keyStorePassword>\n</server>')
              + '<p>&lt;client-secret&gt;Esc-Pa55&lt;/client-secret&gt;</p>')

    # when
    text = page_text(markup)

    # then
    assert '<username>deploy</username>' in text and '<password>[redacted]</password>' in text and '<keyStorePassword>[redacted]</keyStorePassword>' in text
    assert '<client-secret>[redacted]</client-secret>' in text
    for leaked in ('Nexus-Pa55', 'Ks-Pa55', 'Esc-Pa55'):
        assert leaked not in text


@pytest.mark.parametrize('line,secret', [("DB password's: Apo-Pa55", 'Apo-Pa55'), ('Vault token\u2019s: Cur-Pa55', 'Cur-Pa55'), ('passwords: Plu-Pa55', 'Plu-Pa55'),
                                         ('Admin password -> Arr-Pa55', 'Arr-Pa55')])
def test_suffix_forms_of_a_secret_key_are_masked(line, secret):
    # when
    text = page_text(f'<p>{line}</p>')

    # then
    assert secret not in text and text.endswith(' [redacted]')


def test_a_table_with_a_title_row_masks_the_column_under_its_secret_header():
    # given
    markup = ('<table><tbody><tr><th colspan="2"><p><strong>Prod database</strong></p></th></tr>'
              '<tr><th><p>User</p></th><th><p>Password</p></th></tr>'
              '<tr><td><p>app_user</p></td><td><p>Tbl Pa55 word</p></td></tr></tbody></table>')

    # when
    text = page_text(markup)

    # then
    assert text == 'Prod database\n\nUser | Password\n\napp_user | [redacted]'


def test_a_grouped_header_and_a_group_row_keep_the_secret_column_in_place():
    # given
    markup = ('<table><tr><th colspan="2">Account</th><th>Password</th></tr>'
              '<tr><td>app</td><td>db-1</td><td>Grp-Pa55</td></tr>'
              '<tr><th colspan="3">Staging</th></tr>'
              '<tr><th>ops</th><th>db-2</th><th>Th-Pa55</th></tr></table>')

    # when
    text = page_text(markup)

    # then
    assert text == 'Account | Password\n\napp | db-1 | [redacted]\n\nStaging\n\nops | db-2 | [redacted]'


def test_ordinary_words_that_hold_a_key_word_stay_readable():
    # when
    text = page_text('<p>Tests passed: 12</p><p>cache bypass: true</p><p>tokenizer: standard</p><p>passport: AB123</p><p>&lt;bypass&gt;yes&lt;/bypass&gt;</p>')

    # then
    assert text == 'Tests passed: 12\n\ncache bypass: true\n\ntokenizer: standard\n\npassport: AB123\n\n<bypass>yes</bypass>'


def test_a_runbook_pipeline_in_a_code_block_stays_readable():
    # given
    runbook = ('kubectl -n shop get secret db-creds -o yaml | grep host\n'
               'kubectl -n shop rollout restart deploy/checkout-service | tee restart.log\n'
               'kubectl -n shop logs deploy/checkout-service | grep -c ERROR')

    # when
    text = page_text(code(runbook))

    # then
    assert text == runbook


def test_the_page_tool_answer_carries_none_of_these_secrets(store: Store):
    # given
    markup = (code('"spring.datasource.password": "Pr0d-S3cret!"\n<password>Nexus-Pa55</password>') + '<p>DB password: Bo-Pa55!</p>'
              '<table><tr><th colspan="2">Prod</th></tr><tr><th>User</th><th>Password</th></tr><tr><td>app_user</td><td>Tbl-Pa55</td></tr></table>')
    b = wiki_box(store, fake(page=PAGE_CONTENT | {'body': {'storage': {'value': markup, 'representation': 'storage'}}}))

    # when
    out, err = call(b, op='page', id='123456')

    # then
    assert not err
    seen = json.dumps(out, ensure_ascii=False)
    for leaked in ('Pr0d-S3cret!', 'Nexus-Pa55', 'Bo-Pa55!', 'Tbl-Pa55'):
        assert leaked not in seen


def test_the_core_secret_masking_covers_keys_elements_credentials_and_private_keys_and_leaves_the_rest_to_redact():
    # given
    pem = '-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----'
    text = f'password=Hunter22 and <apiKey>K-77</apiKey> at https://ops:Pa55@db.example.com, key {pem}, mail alice@example.com, passport: AB123'

    # when
    out = secrets_masked(text)

    # then
    assert out == 'password=[redacted] and <apiKey>[redacted]</apiKey> at https://***@db.example.com, key [redacted], mail alice@example.com, passport: AB123'
