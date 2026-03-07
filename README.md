# Flix-music

A proof-of-concept Stremio addon that combines:

- **MusicBrainz** for searchable music catalog and metadata.
- **Telegram** for stream delivery (via Bot API `file_id` resolution or pre-generated direct URLs).
- **LinkFilesBot fallback** when a direct stream URL is not yet mapped.

## Overview

This service exposes standard Stremio addon endpoints:

- `manifest`
- `catalog`
- `meta`
- `stream`
- `healthz`

The catalog and metadata are sourced from the MusicBrainz Web Service. Stream URLs are resolved from a mapping keyed by MusicBrainz recording ID.

## Features

- Search recordings from MusicBrainz.
- Return recording metadata (title, artist, cover art URL).
- Resolve playable stream URLs using either:
  - Telegram Bot API `getFile` (`file_id` -> direct file URL), or
  - a pre-resolved `direct_url`.
- Return a LinkFilesBot deep-link as fallback when no mapping exists.

## Requirements

- Python 3.10+
- `fastapi`
- `uvicorn`
- `requests`
- `pytest` (for tests)

Dependencies are listed in `requirements.txt`.

## Installation

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configuration

Set the following environment variables before starting the service.

### Required for Telegram `file_id` resolution

```bash
export TELEGRAM_BOT_TOKEN="123456:ABCDEF"
```

### Recommended for MusicBrainz API usage

```bash
export MUSICBRAINZ_USER_AGENT="FlixMusicAddon/0.2 (contact@example.com)"
```

### Mapping of MusicBrainz recording IDs to Telegram sources

```bash
export TELEGRAM_FILE_MAPPING='{
  "<recording_mbid>": "<file_id_or_direct_url>",
  "<recording_mbid_2>": {
    "file_id": "<telegram_file_id>",
    "direct_url": "https://cdn.example/song.mp3"
  }
}'
```

Supported value formats per recording ID:

1. String `file_id`
   - `{"<mbid>": "AgACAg..."}`
2. String `direct_url`
   - `{"<mbid>": "https://.../song.mp3"}`
3. Object with one or both fields
   - `{"<mbid>": {"file_id": "...", "direct_url": "..."}}`

### Optional LinkFilesBot URL template

```bash
export LINKFILESBOT_URL_TEMPLATE="https://t.me/LinkFilesBot?start={recording_id}"
```

## Running the service

```bash
uvicorn app.main:app --host 0.0.0.0 --port 7000
```

## Endpoints

- `GET /manifest.json`
- `GET /healthz`
- `GET /catalog/movie/musicbrainz-recordings.json?search=metallica`
- `GET /meta/movie/mb:<recording-id>.json`
- `GET /stream/movie/mb:<recording-id>.json`

## Stream Resolution Logic

For `GET /stream/movie/mb:<recording-id>.json`:

1. Look up `<recording-id>` in `TELEGRAM_FILE_MAPPING`.
2. If `direct_url` is present, return it as Stremio stream `url`.
3. Else, if `file_id` is present, call Telegram Bot API `getFile` and return the resolved direct URL.
4. If no mapping exists, return a fallback stream entry using `externalUrl` (LinkFilesBot deep-link).

## Practical Bot Workflow (Example)

1. Locate or prepare the track in Telegram (e.g., via `@vkmusic_bot`).
2. Obtain a direct link or reusable identifier (e.g., via `@LinkFilesBot` or bot-side file handling).
3. Store the result in `TELEGRAM_FILE_MAPPING` under the corresponding MusicBrainz recording ID.
4. Consume `/stream/...` from Stremio.

## Notes and Limitations

- This project is a PoC and does not include persistent storage.
- Source mapping is environment-variable based.
- Telegram access, rate limits, and file availability depend on bot/account setup.
- Legal/licensing compliance for streamed content is the operator’s responsibility.
