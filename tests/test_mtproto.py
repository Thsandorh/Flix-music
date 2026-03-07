from types import SimpleNamespace

from app.mtproto import (
    _extract_urls_from_message,
    _extract_urls_from_text,
    _first_non_telegram_url,
    _first_url_from_messages,
    _is_telegram_url,
)


def _msg(text=None, buttons=None):
    return SimpleNamespace(raw_text=text, buttons=buttons)


def _button(url):
    return SimpleNamespace(url=url)


def test_extract_urls_from_text_multiple():
    urls = _extract_urls_from_text("one https://a.test/x and https://b.test/y")
    assert urls == ["https://a.test/x", "https://b.test/y"]


def test_extract_urls_from_message_reads_buttons():
    message = _msg("no url", buttons=[[_button("https://btn.test/1")]])
    assert _extract_urls_from_message(message) == ["https://btn.test/1"]


def test_first_url_from_messages_keeps_message_order():
    messages = [_msg("none"), _msg("https://first.test"), _msg("https://second.test")]
    assert _first_url_from_messages(messages) == "https://first.test"


def test_first_non_telegram_url_prefers_playable_link():
    messages = [_msg("https://t.me/abc"), _msg("https://cdn.example/song.mp3")]
    assert _first_non_telegram_url(messages) == "https://cdn.example/song.mp3"


def test_is_telegram_url():
    assert _is_telegram_url("https://t.me/abc") is True
    assert _is_telegram_url("https://telegram.me/abc") is True
    assert _is_telegram_url("https://cdn.example/song.mp3") is False
