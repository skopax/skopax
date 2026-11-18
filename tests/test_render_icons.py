import stat

import pytest

from tools import render_icons


def fake_binary(folder, name):
    path = folder / name
    path.write_text('#!/bin/sh\n', encoding='utf-8')
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def test_an_explicit_chrome_path_wins(tmp_path):
    # given
    chosen = fake_binary(tmp_path, 'my-chrome')

    # then
    assert render_icons.find_chrome(chosen) == chosen


def test_an_explicit_path_that_does_not_exist_is_refused(tmp_path):
    # then
    assert render_icons.find_chrome(str(tmp_path / 'missing')) is None


@pytest.mark.parametrize('name', ['google-chrome', 'chromium', 'chromium-browser'])
def test_a_browser_on_the_path_is_found_when_no_app_is_installed(tmp_path, monkeypatch, name):
    # given
    found = fake_binary(tmp_path, name)
    monkeypatch.setattr(render_icons, 'CHROME_CANDIDATES', [])
    monkeypatch.setenv('PATH', str(tmp_path))

    # then
    assert render_icons.find_chrome() == found


def test_nothing_found_is_none(tmp_path, monkeypatch):
    # given
    monkeypatch.setattr(render_icons, 'CHROME_CANDIDATES', [])
    monkeypatch.setenv('PATH', str(tmp_path))

    # then
    assert render_icons.find_chrome() is None
