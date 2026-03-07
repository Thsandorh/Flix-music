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


def test_stream_resolves_via_mtproto(monkeypatch):
    track_id = main._build_track_id(artist="Daft Punk", title="One More Time")

    async def fake_resolver(_query):
        return "https://cdn.example/resolved.mp3"

    monkeypatch.setattr(main, "_find_mapping_entry", lambda _track_ref, _id: None)
    monkeypatch.setattr(main, "resolve_direct_url_from_bots", fake_resolver)

    payload = main.stream("movie", track_id)

    assert payload["streams"][0]["url"] == "https://cdn.example/resolved.mp3"


def test_stream_uses_cached_mtproto_result(monkeypatch):
    track_id = main._build_track_id(artist="Daft Punk", title="One More Time")
    calls = {"count": 0}

    async def fake_resolver(_query):
        calls["count"] += 1
        return "https://cdn.example/cached.mp3"

    monkeypatch.setattr(main, "_find_mapping_entry", lambda _track_ref, _id: None)
    monkeypatch.setattr(main, "resolve_direct_url_from_bots", fake_resolver)
    monkeypatch.setattr(main, "_DIRECT_URL_CACHE", {})

    first = main.stream("movie", track_id)
    second = main.stream("movie", track_id)

    assert first["streams"][0]["url"] == "https://cdn.example/cached.mp3"
    assert second["streams"][0]["url"] == "https://cdn.example/cached.mp3"
    assert calls["count"] == 1
