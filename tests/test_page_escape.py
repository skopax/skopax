import json
import re

from fastapi.testclient import TestClient

from tests.conftest import AUTH, anomaly, state

DATA = re.compile(r'<script type="application/json" id="skopax-data">(.*?)</script>', re.S)


def test_stored_text_that_opens_an_html_comment_and_a_script_cannot_swallow_the_page(client: TestClient):
    # given
    key = 'log|checkout-service|ERROR|No static resource api/<!--<script>x for request'
    client.put('/api/state', json=state(anomaly(key, what='<!--<script>x</script>-->')), headers=AUTH)

    # when
    res = client.get('/')

    # then
    blob = DATA.search(res.text).group(1)
    assert '<' not in blob, 'a "<" in the data script can switch the HTML tokenizer into its escaped script states'
    assert [(a['key'], a['what']) for a in json.loads(blob)['anomalies']] == [(key, '<!--<script>x</script>-->')]
    assert res.text.index('<div class="page">') > res.text.index(blob)
