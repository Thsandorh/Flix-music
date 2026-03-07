import json
import os
from typing import Any

import requests
from fastapi import FastAPI, HTTPException

from app.helpers import build_linkfilesbot_url, env_mapping, safe_artist_string

app = FastAPI(title="Stremio MusicBrainz + Telegram Addon")

MUSICBRAINZ_BASE = "https://musicbrainz.org/ws/2"
USER_AGENT = os.getenv(
    "MUSICBRAINZ_USER_AGENT",
    "FlixMusicStremioAddon/0.2 (you@example.com)",
)


def _mb_get(path: str, params: dict[str, Any]) -> dict[str, Any]:
    response = requests.get(
        f"{MUSICBRAINZ_BASE}/{path}",
        params={**params, "fmt": "json"},
        headers={"User-Agent": USER_AGENT},
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


def _poster_from_release(release_id: str | None) -> str | None:
    if not release_id:
        return None
    return f"https://coverartarchive.org/release/{release_id}/front-250"


def _telegram_file_url(file_id: str) -> str:
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not bot_token:
        raise HTTPException(status_code=500, detail="TELEGRAM_BOT_TOKEN is not configured")

    response = requests.get(
        f"https://api.telegram.org/bot{bot_token}/getFile",
        params={"file_id": file_id},
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json()

    if not payload.get("ok"):
        raise HTTPException(status_code=400, detail=f"Telegram error: {payload}")

    file_path = payload["result"]["file_path"]
    return f"https://api.telegram.org/file/bot{bot_token}/{file_path}"


def _mapping() -> dict[str, dict[str, str]]:
    raw = os.getenv("TELEGRAM_FILE_MAPPING", "{}")
    try:
        return env_mapping(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"Invalid TELEGRAM_FILE_MAPPING JSON: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/manifest.json")
def manifest() -> dict[str, Any]:
    return {
        "id": "community.musicbrainz.telegram",
        "version": "0.2.0",
        "name": "MusicBrainz + Telegram",
        "description": "Music catalog from MusicBrainz and stream links from Telegram files.",
        "resources": ["catalog", "meta", "stream"],
        "types": ["movie"],
        "catalogs": [
            {
                "type": "movie",
                "id": "musicbrainz-recordings",
                "name": "MusicBrainz recordings",
                "extra": [{"name": "search", "isRequired": False}],
            }
        ],
        "idPrefixes": ["mb:"],
    }


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/catalog/{type}/{catalog_id}.json")
def catalog(type: str, catalog_id: str, search: str | None = None) -> dict[str, Any]:
    if type != "movie" or catalog_id != "musicbrainz-recordings":
        raise HTTPException(status_code=404, detail="Catalog not found")

    query = search.strip() if search else "tag:rock"
    data = _mb_get("recording", {"query": query, "limit": 20})

    metas = []
    for rec in data.get("recordings", []):
        rels = rec.get("releases", [])
        first_release = rels[0]["id"] if rels else None
        artist = safe_artist_string(rec.get("artist-credit"))
        metas.append(
            {
                "id": f"mb:{rec['id']}",
                "type": "movie",
                "name": rec.get("title", "Unknown"),
                "poster": _poster_from_release(first_release),
                "description": f"Artist: {artist}" if artist else "",
            }
        )

    return {"metas": metas}


@app.get("/meta/{type}/{id}.json")
def meta(type: str, id: str) -> dict[str, Any]:
    if type != "movie" or not id.startswith("mb:"):
        raise HTTPException(status_code=404, detail="Meta not found")

    mbid = id[3:]
    rec = _mb_get(f"recording/{mbid}", {"inc": "artists+releases"})

    rels = rec.get("releases", [])
    first_release = rels[0]["id"] if rels else None
    artist = safe_artist_string(rec.get("artist-credit"))

    return {
        "meta": {
            "id": id,
            "type": "movie",
            "name": rec.get("title", "Unknown"),
            "poster": _poster_from_release(first_release),
            "description": f"Artist: {artist}" if artist else "",
        }
    }


@app.get("/stream/{type}/{id}.json")
def stream(type: str, id: str) -> dict[str, Any]:
    if type != "movie" or not id.startswith("mb:"):
        raise HTTPException(status_code=404, detail="Stream not found")

    mbid = id[3:]
    entry = _mapping().get(mbid)

    if not entry:
        # Fallback for your workflow: search/prepare in @vkmusic_bot and convert with @LinkFilesBot.
        return {
            "streams": [
                {
                    "title": "No direct file yet - open LinkFilesBot",
                    "externalUrl": build_linkfilesbot_url(mbid),
                }
            ]
        }

    if "direct_url" in entry:
        return {
            "streams": [
                {
                    "title": "Direct URL",
                    "url": entry["direct_url"],
                }
            ]
        }

    direct_url = _telegram_file_url(entry["file_id"])
    return {
        "streams": [
            {
                "title": "Telegram bot file",
                "url": direct_url,
            }
        ]
    }
