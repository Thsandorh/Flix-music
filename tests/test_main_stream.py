import time

import pytest
from fastapi import HTTPException

from app import main


def test_stream_rejects_telegram_direct_url(monkeypatch):
    track_id = main._build_track_id(artist="Daft Punk", title="One More Time")
    monkeypatch.setattr(main, "_find_mapping_entry", lambda _track_ref, _id: {"direct_url": "https://t.me/bad"})

    with pytest.raises(HTTPException) as exc:
        main.stream("movie", track_id)

    assert exc.value.status_code == 502
    assert "Telegram" in str(exc.value.detail)


def test_stream_returns_non_telegram_direct_url(monkeypatch):
    track_id = main._build_track_id(artist="Daft Punk", title="One More Time")
    monkeypatch.setattr(main, "_find_mapping_entry", lambda _track_ref, _id: {"direct_url": "https://cdn.example/a.mp3"})

    payload = main.stream("movie", track_id)
    assert payload["streams"][0]["url"] == "https://cdn.example/a.mp3"


def test_stream_returns_lazy_play_url(monkeypatch):
    track_id = main._build_track_id(artist="Daft Punk", title="One More Time")
    monkeypatch.setattr(main, "_find_mapping_entry", lambda _track_ref, _id: None)
    monkeypatch.setattr(main, "_DIRECT_URL_CACHE", {})

    payload = main.stream("movie", track_id)

    assert payload["streams"][0]["url"] == main._playback_url(track_id)
    assert payload["streams"][0]["url"].endswith('/audio.mp3')


def test_play_resolves_via_mtproto(monkeypatch):
    track_id = main._build_track_id(artist="Daft Punk", title="One More Time")

    async def fake_resolver(_query):
        return "https://cdn.example/resolved.mp3"

    monkeypatch.setattr(main, "_find_mapping_entry", lambda _track_ref, _id: None)
    monkeypatch.setattr(main, "resolve_direct_url_from_bots", fake_resolver)
    monkeypatch.setattr(main, "_DIRECT_URL_CACHE", {})

    response = main.play(main._encode_play_token(track_id), 'audio.mp3')

    assert response.status_code == 302
    assert response.headers["location"] == "https://cdn.example/resolved.mp3"


def test_stream_uses_cached_mtproto_result(monkeypatch):
    track_id = main._build_track_id(artist="Daft Punk", title="One More Time")
    monkeypatch.setattr(main, "_find_mapping_entry", lambda _track_ref, _id: None)
    monkeypatch.setattr(main, "_DIRECT_URL_CACHE", {track_id: (time.time() + 60, "https://cdn.example/cached.mp3")})

    payload = main.stream("movie", track_id)

    assert payload["streams"][0]["url"] == "https://cdn.example/cached.mp3"
