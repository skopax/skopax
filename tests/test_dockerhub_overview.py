import pytest

from tools import dockerhub_overview as overview


def test_the_overview_names_the_release_and_its_minor_tag():
    # given
    version = '0.5.10'
    # when
    text = overview.render(version)
    # then
    assert '`0.5.10` exact release, `0.5` latest patch of 0.5' in text
    assert 'skopax/skopax:0.5\n' in text
    assert '{' not in text.replace('${', '')
    assert len(text) <= overview.LIMIT
    assert text.isascii()


def test_an_overview_without_the_version_mark_is_refused(tmp_path):
    # given
    path = tmp_path / 'dockerhub.md'
    path.write_text('# Skopax\n', encoding='utf-8')
    # when / then
    with pytest.raises(ValueError):
        overview.render('0.5.10', path)


def test_a_failed_update_warns_and_never_fails_the_release(monkeypatch, capsys):
    # given
    monkeypatch.setenv('HUB_USER', 'someone')
    monkeypatch.setenv('HUB_TOKEN', 'not-a-real-token')
    def refuse(*args, **kwargs):
        raise OSError('offline')
    monkeypatch.setattr(overview, 'call', refuse)
    # when
    code = overview.main(['0.5.10'])
    # then
    assert code == 0
    err = capsys.readouterr().err
    assert '::warning::Docker Hub overview not updated: OSError' in err
    assert 'not-a-real-token' not in err


def test_check_renders_without_calling_docker_hub(monkeypatch, capsys):
    # given
    monkeypatch.setattr(overview, 'call', lambda *a, **k: pytest.fail('no network in check'))
    # when
    code = overview.main(['0.5.10', '--check'])
    # then
    assert code == 0
    assert 'overview renders for 0.5.10' in capsys.readouterr().out
