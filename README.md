# Flix-music (Stremio + MusicBrainz + Telegram)

Igen, megvalósítható a flow amit írtál:

- keresés/meta: **MusicBrainz API**
- zene forrás: Telegram (pl. `@vkmusic_bot`)
- direkt fájl link: `@LinkFilesBot` (vagy `file_id` alapú feloldás saját bot tokennel)
- lejátszás Stremióban: `/stream/...` endpoint `url` mezőn keresztül

## Mit tud ez a verzió?

- `catalog` + `meta`: MusicBrainz recording adatok (név, artist, borító)
- `stream`: 2 mód
  1. **direct_url** alapján (ha már van kész link pl. LinkFilesBot-ból)
  2. **file_id** alapján (`getFile` Telegram Bot API -> direkt fájl URL)
- fallbackként (ha még nincs mapping): `externalUrl`-t ad `@LinkFilesBot` deep linkre

## Környezeti változók

```bash
export TELEGRAM_BOT_TOKEN="123456:ABCDEF"
export MUSICBRAINZ_USER_AGENT="FlixMusicAddon/0.2 (you@example.com)"

# Formátum 1: egyszerű string (file_id)
# {"<recording_id>": "<file_id>"}

# Formátum 2: egyszerű string (direct_url)
# {"<recording_id>": "https://.../song.mp3"}

# Formátum 3: objektum
# {"<recording_id>": {"file_id": "...", "direct_url": "..."}}

export TELEGRAM_FILE_MAPPING='{
  "f4d5f6bb-4f95-4a20-9f0a-99f9e1f5f111": {
    "direct_url": "https://example-cdn.local/song.mp3"
  }
}'

# opcionális
export LINKFILESBOT_URL_TEMPLATE="https://t.me/LinkFilesBot?start={recording_id}"
```

## Telepítés / futtatás

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 7000
```

## Endpointok

- `GET /manifest.json`
- `GET /healthz`
- `GET /catalog/movie/musicbrainz-recordings.json?search=metallica`
- `GET /meta/movie/mb:<recording-id>.json`
- `GET /stream/movie/mb:<recording-id>.json`

## Gyakorlati workflow (a botjaiddal)

1. zenét keresel `@vkmusic_bot`-ban
2. a fájlt továbbítod/kezeled `@LinkFilesBot`-tal, ahonnan kapsz közvetlen linket
3. a recording MBID-hez beírod `direct_url`-ként a `TELEGRAM_FILE_MAPPING`-be
4. Stremio a `/stream` endpointból lejátsza a kapott linket

> Megjegyzés: a botok működése, rate limit, fájl-hozzáférés és jogi/licenc kérdések a te Telegram setupodtól függenek.
