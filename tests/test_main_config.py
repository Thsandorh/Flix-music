from app import main


def test_manifest_without_user_has_public_catalogs_only():
    payload = main.manifest()
    catalog_ids = [item['id'] for item in payload['catalogs']]
    assert catalog_ids == ['lastfm-top', 'lastfm-trending', 'lastfm-search']


def test_configured_manifest_adds_user_catalogs():
    token = main._encode_config('sandor555')
    payload = main.configured_manifest(token)
    catalog_ids = [item['id'] for item in payload['catalogs']]
    assert 'lastfm-user-loved' in catalog_ids
    assert 'lastfm-user-recent' in catalog_ids
    assert 'lastfm-user-top' in catalog_ids


def test_catalog_user_loved_uses_lastfm_username(monkeypatch):
    calls = []

    def fake_user_loved(username):
        calls.append(username)
        return [{'name': 'One More Time', 'artist': {'name': 'Daft Punk'}}]

    monkeypatch.setattr(main, '_user_loved_tracks', fake_user_loved)
    payload = main._catalog_payload('movie', 'lastfm-user-loved', lastfm_user='sandor555')

    assert calls == ['sandor555']
    assert payload['metas'][0]['name'] == 'One More Time'


def test_configured_catalog_uses_token_user(monkeypatch):
    calls = []

    def fake_user_recent(username):
        calls.append(username)
        return [{'name': 'Aerodynamic', 'artist': {'name': 'Daft Punk'}}]

    monkeypatch.setattr(main, '_user_recent_tracks', fake_user_recent)
    token = main._encode_config('sandor555')
    payload = main.configured_catalog(token, 'movie', 'lastfm-user-recent')

    assert calls == ['sandor555']
    assert payload['metas'][0]['name'] == 'Aerodynamic'


def test_configure_page_contains_working_markup():
    html = main.configure()
    assert '<input id="lastfmUser"' in html
    assert 'onclick="buildUrl()"' in html
    assert '\\"' not in html
