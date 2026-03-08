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


def test_stream_returns_direct_url_from_config(monkeypatch):
    track_id = main._build_track_id(artist="Daft Punk", title="One More Time")
    monkeypatch.setattr(main, "_find_mapping_entry", lambda _track_ref, _id: {"direct_url": "https://cdn.example/a.mp3"})

    payload = main.stream("movie", track_id)
    assert payload["streams"][0]["url"] == "https://cdn.example/a.mp3"
    assert payload["streams"][0]["behaviorHints"]["notWebReady"] is True


def test_stream_returns_direct_url_when_resolution_succeeds(monkeypatch):
    track_id = main._build_track_id(artist="Daft Punk", title="One More Time")
    monkeypatch.setattr(main, "_resolve_direct_stream_url", lambda _id: "https://cdn.example/resolved.mp3")

    payload = main.stream("movie", track_id)

    assert payload["streams"][0]["url"] == "https://cdn.example/resolved.mp3"
    assert payload["streams"][0]["behaviorHints"]["notWebReady"] is True


def test_stream_returns_empty_when_resolution_fails(monkeypatch):
    track_id = main._build_track_id(artist="Daft Punk", title="One More Time")

    def boom(_id):
        raise HTTPException(status_code=502, detail="No result link or document found in search bot response")

    monkeypatch.setattr(main, "_resolve_direct_stream_url", boom)

    payload = main.stream("movie", track_id)

    assert payload == {"streams": []}


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


def test_stream_uses_cached_mtproto_result(monkeypatch):
    track_id = main._build_track_id(artist="Daft Punk", title="One More Time")
    monkeypatch.setattr(main, "_DIRECT_URL_CACHE", {track_id: (time.time() + 60, "https://cdn.example/cached.mp3")})

    payload = main.stream("movie", track_id)

    assert payload["streams"][0]["url"] == "https://cdn.example/cached.mp3"
    assert payload["streams"][0]["behaviorHints"]["notWebReady"] is True

def test_expand_direct_stream_url_resolves_shortlink(monkeypatch):
    monkeypatch.setattr(main, "_resolve_shortlink_once", lambda url, proxy_url="": "https://site--linkfilesbot--gb24qxlnkkt9.code.run/download/1727945" if not proxy_url else "")

    assert main._expand_direct_stream_url("https://clck.ru/test") == "https://site--linkfilesbot--gb24qxlnkkt9.code.run/download/1727945"



def test_expand_direct_stream_url_keeps_shortlink_when_target_stays_shortener(monkeypatch):
    monkeypatch.setattr(main, "_resolve_shortlink_once", lambda url, proxy_url="": "")
    monkeypatch.setattr(main, "_acquire_shortlink_proxy", lambda: None)

    assert main._expand_direct_stream_url("https://clck.ru/test") == "https://clck.ru/test"



def test_expand_shortlink_with_curl_returns_non_shortener(monkeypatch):
    class FakeCompletedProcess:
        returncode = 0
        stdout = "https://cdn.example/final.mp3"

    monkeypatch.setattr(main.shutil, "which", lambda name: "curl.exe")
    monkeypatch.setattr(main.subprocess, "run", lambda *args, **kwargs: FakeCompletedProcess())

    assert main._expand_shortlink_with_curl("https://clck.ru/test") == "https://cdn.example/final.mp3"



def test_expand_direct_stream_url_accepts_non_shortener_from_curl(monkeypatch):
    monkeypatch.setattr(main, "_resolve_shortlink_once", lambda url, proxy_url="": "https://cdn.example/final.mp3" if not proxy_url else "")

    assert main._expand_direct_stream_url("https://clck.ru/test") == "https://cdn.example/final.mp3"



def test_expand_direct_stream_url_rejects_blocked_curl_target_and_uses_fallback(monkeypatch):
    attempts = []

    def fake_resolve(url, proxy_url=""):
        attempts.append(proxy_url)
        if not proxy_url:
            return ""
        return "https://cdn.example/final.mp3"

    proxy = main.ShortlinkProxyEndpoint(proxy_id="proxy-1", proxy_url="http://proxy.example:8080", label="proxy")
    monkeypatch.setattr(main, "_resolve_shortlink_once", fake_resolve)
    monkeypatch.setattr(main, "_acquire_shortlink_proxy", lambda: proxy)
    monkeypatch.setattr(main, "_shortlink_proxy_max_attempts", lambda: 1)

    assert main._expand_direct_stream_url("https://clck.ru/test") == "https://cdn.example/final.mp3"
    assert attempts == ["", "http://proxy.example:8080"]



def test_extract_shortlink_target_yandex_redirect():
    url = "https://sba.yandex.ru/redirect?url=https%3A%2F%2Fsite--linkfilesbot--gb24qxlnkkt9.code.run%2Fdownload%2F1727896"
    assert main._extract_shortlink_target(url) == "https://site--linkfilesbot--gb24qxlnkkt9.code.run/download/1727896"


def test_shortlink_proxy_enabled_defaults_to_true(monkeypatch):
    monkeypatch.delenv("SHORTLINK_PROXY_ENABLED", raising=False)
    monkeypatch.delenv("PROVIDER_PROXY_POOL_ENABLED", raising=False)

    assert main._shortlink_proxy_enabled() is True


def test_expand_direct_stream_url_marks_proxy_failure(monkeypatch):
    failures = []
    proxy = main.ShortlinkProxyEndpoint(proxy_id="proxy-1", proxy_url="http://proxy.example:8080", label="proxy")

    monkeypatch.setattr(main, "_resolve_shortlink_once", lambda url, proxy_url="": "")
    monkeypatch.setattr(main, "_acquire_shortlink_proxy", lambda: proxy)
    monkeypatch.setattr(main, "_shortlink_proxy_max_attempts", lambda: 1)
    monkeypatch.setattr(main, "_mark_shortlink_proxy_failure", lambda endpoint: failures.append(endpoint.proxy_id))

    assert main._expand_direct_stream_url("https://clck.ru/test") == "https://clck.ru/test"
    assert failures == ["proxy-1"]


def test_stream_expands_shortlink_to_final_media_url(monkeypatch):
    track_id = main._build_track_id(artist="Daft Punk", title="One More Time")

    async def fake_resolver(_query):
        return "https://clck.ru/test"

    monkeypatch.setattr(main, "_find_mapping_entry", lambda _track_ref, _id: None)
    monkeypatch.setattr(main, "resolve_direct_url_from_bots", fake_resolver)
    monkeypatch.setattr(main, "_expand_direct_stream_url", lambda url: "https://site--linkfilesbot--gb24qxlnkkt9.code.run/download/1727945")
    monkeypatch.setattr(main, "_DIRECT_URL_CACHE", {})

    payload = main.stream("movie", track_id)

    assert payload["streams"][0]["url"] == "https://site--linkfilesbot--gb24qxlnkkt9.code.run/download/1727945"
    assert payload["streams"][0]["behaviorHints"]["notWebReady"] is True


