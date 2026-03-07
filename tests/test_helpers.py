import pytest

from app.helpers import (
    build_direct_download_bot_url,
    build_linkfilesbot_url,
    build_recording_search_query,
    build_telegram_search_url,
    env_mapping,
    has_telegram_app_credentials,
    safe_artist_string,
)


def test_env_mapping_supports_direct_url_string():
    data = env_mapping('{"mbid":"https://cdn.example/song.mp3"}')
    assert data == {"mbid": {"direct_url": "https://cdn.example/song.mp3"}}


def test_env_mapping_supports_message_url_string():
    data = env_mapping('{"mbid":"https://t.me/c/123/456"}')
    assert data == {"mbid": {"message_url": "https://t.me/c/123/456"}}


def test_env_mapping_supports_object_format():
    data = env_mapping('{"mbid":{"direct_url":"https://a","message_url":"https://t.me/a"}}')
    assert data == {"mbid": {"direct_url": "https://a", "message_url": "https://t.me/a"}}


def test_env_mapping_rejects_non_url_string():
    with pytest.raises(ValueError):
        env_mapping('{"mbid":"AgACAg..."}')


def test_env_mapping_rejects_invalid_type():
    with pytest.raises(ValueError):
        env_mapping('{"mbid":123}')


def test_build_linkfilesbot_url():
    assert build_linkfilesbot_url("id123", "https://t.me/LinkFilesBot?start={recording_id}") == (
        "https://t.me/LinkFilesBot?start=id123"
    )


def test_build_telegram_search_url_encodes_query():
    url = build_telegram_search_url("Daft Punk - One More Time 2000")
    assert "Daft+Punk+-+One+More+Time+2000" in url


def test_build_recording_search_query():
    assert build_recording_search_query("One More Time", "Daft Punk", "2000") == "Daft Punk - One More Time 2000"


def test_safe_artist_string():
    assert safe_artist_string([{"name": "A"}, " / ", {"name": "B"}]) == "A, B"
    assert safe_artist_string(None) == ""


def test_has_telegram_app_credentials(monkeypatch):
    monkeypatch.delenv("TELEGRAM_API_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_API_HASH", raising=False)
    assert has_telegram_app_credentials() is False

    monkeypatch.setenv("TELEGRAM_API_ID", "123")
    monkeypatch.setenv("TELEGRAM_API_HASH", "abc")
    assert has_telegram_app_credentials() is True


def test_build_direct_download_bot_url_encodes_query():
    url = build_direct_download_bot_url("Metallica Nothing Else Matters")
    assert "Metallica+Nothing+Else+Matters" in url
