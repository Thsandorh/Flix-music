import pytest
from fastapi import HTTPException

from app import main


def test_stream_rejects_telegram_direct_url(monkeypatch):
    monkeypatch.setattr(main, "_mapping", lambda: {"abc": {"direct_url": "https://t.me/bad"}})

    with pytest.raises(HTTPException) as exc:
        main.stream("movie", "mb:abc")

    assert exc.value.status_code == 502
    assert "Telegram" in str(exc.value.detail)


def test_stream_returns_non_telegram_direct_url(monkeypatch):
    monkeypatch.setattr(main, "_mapping", lambda: {"abc": {"direct_url": "https://cdn.example/a.mp3"}})

    payload = main.stream("movie", "mb:abc")
    assert payload["streams"][0]["url"] == "https://cdn.example/a.mp3"
