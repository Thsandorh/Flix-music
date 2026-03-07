import time

import pytest
from fastapi import HTTPException

from app import main


def test_api_music_media_rejects_telegram_direct_url(monkeypatch):
    track_id = main._build_track_id(artist="Daft Punk", title="One More Time")
    monkeypatch.setattr(main, "_find_mapping_entry", lambda _track_ref, _id: {"direct_url": "https://t.me/bad"})

    with pytest.raises(HTTPException) as exc:
        main.api_music_media(main._encode_play_token(track_id))

    assert exc.value.status_code == 502
    assert "Telegram" in str(exc.value.detail)


def test_stream_returns_proxy_url(monkeypatch):
    track_id = main._build_track_id(artist="Daft Punk", title="One More Time")
    monkeypatch.setattr(main, "_find_mapping_entry", lambda _track_ref, _id: {"direct_url": "https://cdn.example/a.mp3"})

    payload = main.stream("movie", track_id)
    assert payload["streams"][0]["url"] == main._playback_url(track_id)
    assert payload["streams"][0]["behaviorHints"]["notWebReady"] is True


def test_stream_returns_proxy_url_without_resolving(monkeypatch):
    track_id = main._build_track_id(artist="Daft Punk", title="One More Time")
    monkeypatch.setattr(main, "_find_mapping_entry", lambda _track_ref, _id: None)
    monkeypatch.setattr(main, "_DIRECT_URL_CACHE", {})

    payload = main.stream("movie", track_id)

    assert payload["streams"][0]["url"] == main._playback_url(track_id)
    assert payload["streams"][0]["behaviorHints"]["notWebReady"] is True


def test_play_resolves_via_mtproto(monkeypatch):
    track_id = main._build_track_id(artist="Daft Punk", title="One More Time")

    async def fake_resolver(_query):
        return "https://cdn.example/resolved.mp3"

    monkeypatch.setattr(main, "_find_mapping_entry", lambda _track_ref, _id: None)
    monkeypatch.setattr(main, "resolve_direct_url_from_bots", fake_resolver)
    monkeypatch.setattr(main, "_DIRECT_URL_CACHE", {})

    response = main.api_music_media(main._encode_play_token(track_id))

    assert response.status_code == 302
    assert response.headers["location"] == "https://cdn.example/resolved.mp3"


def test_stream_uses_proxy_url_even_with_cached_mtproto_result(monkeypatch):
    track_id = main._build_track_id(artist="Daft Punk", title="One More Time")
    monkeypatch.setattr(main, "_find_mapping_entry", lambda _track_ref, _id: None)
    monkeypatch.setattr(main, "_DIRECT_URL_CACHE", {track_id: (time.time() + 60, "https://cdn.example/cached.mp3")})

    payload = main.stream("movie", track_id)

    assert payload["streams"][0]["url"] == main._playback_url(track_id)
    assert payload["streams"][0]["behaviorHints"]["notWebReady"] is True

def test_expand_direct_stream_url_resolves_shortlink(monkeypatch):
    class FakeResponse:
        headers = {
            "Location": "https://sba.yandex.ru/redirect?url=https%3A%2F%2Fsite--linkfilesbot--gb24qxlnkkt9.code.run%2Fdownload%2F1727896"
        }

        def close(self):
            return None

    class FakeSession:
        def get(self, url, allow_redirects, stream, headers, timeout):
            assert url == "https://clck.ru/test"
            assert allow_redirects is False
            assert stream is True
            return FakeResponse()

    monkeypatch.setattr(main, "_session", FakeSession())

    assert main._expand_direct_stream_url("https://clck.ru/test") == "https://site--linkfilesbot--gb24qxlnkkt9.code.run/download/1727896"


def test_stream_expands_shortlink_to_final_media_url(monkeypatch):
    track_id = main._build_track_id(artist="Daft Punk", title="One More Time")

    async def fake_resolver(_query):
        return "https://clck.ru/test"

    monkeypatch.setattr(main, "_find_mapping_entry", lambda _track_ref, _id: None)
    monkeypatch.setattr(main, "resolve_direct_url_from_bots", fake_resolver)
    monkeypatch.setattr(main, "_expand_direct_stream_url", lambda url: "https://site--linkfilesbot--gb24qxlnkkt9.code.run/download/1727896")
    monkeypatch.setattr(main, "_DIRECT_URL_CACHE", {})

    payload = main.stream("movie", track_id)

    assert payload["streams"][0]["url"] == main._playback_url(track_id)
    assert payload["streams"][0]["behaviorHints"]["notWebReady"] is True

def test_expand_direct_stream_url_yandex_redirect_passthrough():
    url = "https://sba.yandex.ru/redirect?url=https%3A%2F%2Fsite--linkfilesbot--gb24qxlnkkt9.code.run%2Fdownload%2F1727896"
    assert main._expand_direct_stream_url(url) == "https://site--linkfilesbot--gb24qxlnkkt9.code.run/download/1727896"

