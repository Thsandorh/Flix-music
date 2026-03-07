# Flix-music

Production-oriented Stremio addon (FastAPI) that uses:

- **MusicBrainz** as catalog/metadata provider (equivalent role to TMDB in movie addons).
- **Telegram** as playback target provider (direct links, message links, and metadata-based search fallback).

## How this maps to Stremio architecture

For Stremio, three endpoints define the full experience:

1. `catalog` → what users browse/search in the UI.
2. `meta` → details for a selected catalog item.
3. `stream` → playable target(s) for that item.

In this addon:

- `catalog` and `meta` are assembled from **MusicBrainz recording data**.
- `catalog` has three lanes:
  - `musicbrainz-popular` → query: `tag:pop OR tag:rock`
  - `musicbrainz-recent` → query: `date:[2023 TO *]`
  - `musicbrainz-search` → query from `search` extra (or fallback `tag:music`)
- `stream` uses Telegram links from mapping, and if absent, builds a **Telegram search query by name (+ year)**.

## Key behavior requested

No bot token is required.

When a recording has no direct mapping, stream fallback is generated from MusicBrainz metadata using:

- Artist name
- Track title
- Release year (if available)

Example generated search query:

`Daft Punk - One More Time 2000`

## Endpoints

- `GET /manifest.json`
- `GET /healthz`
- `GET /catalog/movie/musicbrainz-popular.json`
- `GET /catalog/movie/musicbrainz-recent.json`
- `GET /catalog/movie/musicbrainz-search.json?search=metallica`
- `GET /meta/movie/mb:<recording-id>.json`
- `GET /stream/movie/mb:<recording-id>.json`

## Environment variables

### Required

```bash
MUSICBRAINZ_USER_AGENT="FlixMusicStremioAddon/0.5 (your-contact@example.com)"
TELEGRAM_API_ID="37880630"
TELEGRAM_API_HASH="bc02a89bf14722bf6dec1f69c0c3442f"
TELEGRAM_FILE_MAPPING='{}'
```

### Optional tuning (recommended for production)

```bash
MUSICBRAINZ_BASE="https://musicbrainz.org/ws/2"
MUSICBRAINZ_SEARCH_LIMIT="20"
HTTP_TIMEOUT_SECONDS="15"
MB_CACHE_TTL_SECONDS="120"
LOG_LEVEL="INFO"
```

### Optional Telegram URL templates

```bash
# fallback when recording has no mapping and metadata lookup fails
LINKFILESBOT_URL_TEMPLATE="https://t.me/LinkFilesBot?start={recording_id}"

# metadata-based fallback search URL
# placeholders: {query}, {query_encoded}
TELEGRAM_SEARCH_URL_TEMPLATE="https://t.me/vkmusic_bot?start={query_encoded}"
```

### Mapping schema (`TELEGRAM_FILE_MAPPING`)

Mapping key = MusicBrainz recording ID.

```json
{
  "f4d5f6bb-4f95-4a20-9f0a-99f9e1f5f111": {
    "direct_url": "https://cdn.example.com/song.mp3"
  },
  "9cb73623-c4ff-4e5f-b1a0-4b7e6f3140da": {
    "message_url": "https://t.me/c/123456/789"
  }
}
```

Supported forms:

- URL string:
  - `https://...` → `direct_url`
  - `https://t.me/...` → `message_url`
- Object:
  - `{ "direct_url": "...", "message_url": "..." }`

## Local run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 7000
```

## Vercel deployment

This repository includes:

- `api/index.py` (ASGI entrypoint)
- `vercel.json` (routes/build)

Steps:

1. Import repository into Vercel.
2. Configure required env vars.
3. Deploy.
4. Verify:
   - `/healthz`
   - `/manifest.json`

## Reliability hardening included

- HTTP retries for transient MusicBrainz errors (429/5xx).
- Configurable request timeout.
- In-memory TTL cache for MusicBrainz responses.
- Explicit 502 response mapping for upstream request failures.
- Health endpoint includes credential and mapping visibility.

## Notes

- This is still a lightweight service (no persistent DB/admin UI).
- For high-scale production, use external cache/storage and observability.
- Content licensing/compliance remains operator responsibility.
