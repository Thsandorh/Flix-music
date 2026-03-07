import pytest

from app.helpers import build_linkfilesbot_url, env_mapping, safe_artist_string


def test_env_mapping_supports_legacy_file_id_string():
    data = env_mapping('{"mbid":"abc-file-id"}')
    assert data == {"mbid": {"file_id": "abc-file-id"}}


def test_env_mapping_supports_direct_url_string():
    data = env_mapping('{"mbid":"https://cdn.example/song.mp3"}')
    assert data == {"mbid": {"direct_url": "https://cdn.example/song.mp3"}}


def test_env_mapping_supports_object_format():
    data = env_mapping('{"mbid":{"file_id":"x","direct_url":"https://a"}}')
    assert data == {"mbid": {"file_id": "x", "direct_url": "https://a"}}


def test_env_mapping_rejects_invalid_type():
    with pytest.raises(ValueError):
        env_mapping('{"mbid":123}')


def test_build_linkfilesbot_url():
    assert build_linkfilesbot_url("id123", "https://t.me/LinkFilesBot?start={recording_id}") == (
        "https://t.me/LinkFilesBot?start=id123"
    )


def test_safe_artist_string():
    assert safe_artist_string([{"name": "A"}, " / ", {"name": "B"}]) == "A, B"
    assert safe_artist_string(None) == ""
