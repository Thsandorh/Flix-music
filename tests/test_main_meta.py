from app import main


def test_build_meta_item_enriched_fields():
    track = {
        "name": "One More Time",
        "duration": "320000",
        "mbid": "abc-mbid",
        "artist": {"name": "Daft Punk"},
        "album": {"title": "Discovery", "published": "30 Nov 2000, 00:00"},
        "toptags": {"tag": [{"name": "house"}, {"name": "electronic"}]},
        "image": [{"size": "large", "#text": "https://img.example/cover.jpg"}],
        "listeners": "12345",
    }

    meta = main._build_meta_item(track)

    assert meta["name"] == "One More Time"
    assert meta["poster"] == "https://img.example/cover.jpg"
    assert meta["releaseInfo"] == "2000"
    assert meta["genres"] == ["house", "electronic"]
    assert meta["cast"] == ["Daft Punk"]


def test_meta_fallback_when_lastfm_fails(monkeypatch):
    track_id = main._build_track_id(artist="Daft Punk", title="One More Time", year="2000")

    def boom(_track_ref):
        raise main.requests.RequestException("upstream down")

    monkeypatch.setattr(main, "_track_info", boom)
    payload = main.meta("movie", track_id)
    meta = payload["meta"]

    assert meta["name"] == "Daft Punk - One More Time 2000"
    assert "temporarily unavailable" in meta["description"]
