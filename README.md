# Flix-music

Production-oriented Stremio addon (FastAPI) with:

- **MusicBrainz** for catalog and metadata.
- **MTProto bot chain** for automatic Telegram direct-link resolution.

## Core behavior

When a direct URL is not already mapped for a MusicBrainz recording, the addon does **not** return browser bot links.

Instead, it resolves a playable stream URL automatically using MTProto:

1. Send user search phrase (or metadata-derived query) to `@vkmusic_bot`.
2. Take the returned candidate.
3. Send that candidate to direct-download bot (`@LinkFilesBot` by default).
4. Extract direct playable URL from bot response.
5. Return that URL in `/stream` as Stremio `url`.

## Endpoints

- `GET /manifest.json`
- `GET /healthz`
- `GET /catalog/movie/musicbrainz-popular.json`
- `GET /catalog/movie/musicbrainz-recent.json`
- `GET /catalog/movie/musicbrainz-search.json?search=metallica`
- `GET /meta/movie/mb:<recording-id>.json`
- `GET /stream/movie/mb:<recording-id>.json`

## Required environment variables

```bash
MUSICBRAINZ_USER_AGENT="FlixMusicStremioAddon/0.8 (your-contact@example.com)"
TELEGRAM_API_ID="123456"
TELEGRAM_API_HASH="xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
TELEGRAM_STRING_SESSION="<telethon_string_session>"
# or point to an existing Telethon sqlite session file
TELEGRAM_SESSION_PATH="~/telegram_bridge/flix_session"
```

## Optional environment variables

```bash
MUSICBRAINZ_BASE="https://musicbrainz.org/ws/2"
MUSICBRAINZ_SEARCH_LIMIT="20"
HTTP_TIMEOUT_SECONDS="15"
MB_CACHE_TTL_SECONDS="120"
LOG_LEVEL="INFO"
SEARCH_HINT_TTL_SECONDS="1800"

# bot usernames (without @ is also accepted)
VKMUSIC_BOT_USERNAME="vkmusic_bot"
DIRECT_DOWNLOAD_BOT_USERNAME="LinkFilesBot"

# bot response wait interval
MT_PROTO_WAIT_SECONDS="6"
```

## Optional static mapping

If a direct link is already known, it can be hardcoded in `HARDCODED_TELEGRAM_MAPPING` (`app/main.py`) or set via `TELEGRAM_FILE_MAPPING`.

```json
{
  "<musicbrainz-recording-id>": {
    "direct_url": "https://cdn.example/song.mp3"
  }
}
```

## Generate Telegram session

Use the helper script when you need a fresh `TELEGRAM_STRING_SESSION`:

```powershell
python scripts\generate_telegram_session.py --api-id 123456 --api-hash your_telegram_api_hash
```

Optional non-interactive inputs:

```powershell
$env:TELEGRAM_PHONE="+36123456789"
$env:TELEGRAM_LOGIN_CODE="12345"
$env:TELEGRAM_2FA_PASSWORD="your-password"
python scripts\generate_telegram_session.py --api-id 123456 --api-hash your_telegram_api_hash
```

## Local run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 7000
```

## Vercel deployment

Repository includes:

- `api/index.py` (ASGI entrypoint)
- `vercel.json` (routes/build)

Deploy steps:

1. Import repository into Vercel.
2. Set required env vars.
3. Deploy.
4. Verify `/healthz` and `/manifest.json`.

## Notes

- MTProto flow requires a valid authenticated user `TELEGRAM_STRING_SESSION`.
- Stream resolution returns playable `url` links (no `externalUrl` browser handoff).
- For scale, replace in-memory caches with external storage.
