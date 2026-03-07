from app import main


def test_build_meta_item_enriched_fields():
    rec = {
        "id": "abc",
        "title": "One More Time",
        "length": 320000,
        "artist-credit": [{"name": "Daft Punk"}],
        "releases": [{"id": "rel1", "date": "2000-11-30"}],
        "tags": [{"name": "house"}, {"name": "electronic"}],
    }

    meta = main._build_meta_item(id_value="mb:abc", rec=rec)

    assert meta["name"] == "One More Time"
    assert meta["poster"].endswith("/release/rel1/front-250")
    assert meta["releaseInfo"] == "2000-11-30"
    assert meta["genres"] == ["house", "electronic"]
    assert meta["cast"] == ["Daft Punk"]


def test_meta_fallback_when_musicbrainz_fails(monkeypatch):
    def boom(path, params):
        raise main.requests.RequestException("upstream down")

    monkeypatch.setattr(main, "_mb_get", boom)
    main._SEARCH_HINTS["abc"] = (10**12, "Daft Punk - One More Time")

    payload = main.meta("movie", "mb:abc")
    meta = payload["meta"]

    assert meta["name"] == "Daft Punk - One More Time"
    assert "temporarily unavailable" in meta["description"]
