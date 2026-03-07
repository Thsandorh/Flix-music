import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import requests
from fastapi import FastAPI, HTTPException
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app.helpers import (
    build_direct_download_bot_url,
    build_linkfilesbot_url,
    build_recording_search_query,
    build_telegram_search_url,
    env_mapping,
    has_telegram_app_credentials,
    safe_artist_string,
)

logger = logging.getLogger("flix_music")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))


@dataclass(frozen=True)
class Settings:
    musicbrainz_base: str = os.getenv("MUSICBRAINZ_BASE", "https://musicbrainz.org/ws/2")
    user_agent: str = os.getenv("MUSICBRAINZ_USER_AGENT", "FlixMusicStremioAddon/0.5 (contact@example.com)")
    search_limit: int = int(os.getenv("MUSICBRAINZ_SEARCH_LIMIT", "20"))
    timeout_s: int = int(os.getenv("HTTP_TIMEOUT_SECONDS", "15"))
    cache_ttl_s: int = int(os.getenv("MB_CACHE_TTL_SECONDS", "120"))


SETTINGS = Settings()

if "contact@" in SETTINGS.user_agent or "example" in SETTINGS.user_agent:
    logger.warning("MUSICBRAINZ_USER_AGENT should be customized for production deployments.")

app = FastAPI(title="Stremio MusicBrainz + Telegram Addon")

_session = requests.Session()
_retry = Retry(
    total=3,
    connect=3,
    read=3,
    backoff_factor=0.3,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET"],
)
_session.mount("https://", HTTPAdapter(max_retries=_retry))
_session.mount("http://", HTTPAdapter(max_retries=_retry))

# very small in-memory cache: key -> (expires_at, payload)
_MB_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}

# Optional built-in static mapping. Add MBID -> direct/message URL entries here.
# This allows operation without TELEGRAM_FILE_MAPPING env.
HARDCODED_TELEGRAM_MAPPING: dict[str, dict[str, str]] = {
    # "<musicbrainz-recording-id>": {"direct_url": "https://..."},
    # "<musicbrainz-recording-id>": {"message_url": "https://t.me/..."},
}

# search context cache: recording_mbid -> user-entered search phrase
_SEARCH_HINTS: dict[str, tuple[float, str]] = {}
_SEARCH_HINT_TTL_SECONDS = int(os.getenv("SEARCH_HINT_TTL_SECONDS", "1800"))


def _cache_key(path: str, params: dict[str, Any]) -> str:
    ordered = "&".join(f"{k}={params[k]}" for k in sorted(params))
    return f"{path}?{ordered}"


def _mb_get(path: str, params: dict[str, Any]) -> dict[str, Any]:
    req_params = {**params, "fmt": "json"}
    key = _cache_key(path, req_params)
    now = time.time()

    cached = _MB_CACHE.get(key)
    if cached and cached[0] > now:
        return cached[1]

    response = _session.get(
        f"{SETTINGS.musicbrainz_base}/{path}",
        params=req_params,
        headers={"User-Agent": SETTINGS.user_agent},
        timeout=SETTINGS.timeout_s,
    )
    response.raise_for_status()
    payload = response.json()
    _MB_CACHE[key] = (now + SETTINGS.cache_ttl_s, payload)
    return payload


def _poster_from_release(release_id: str | None) -> str | None:
    if not release_id:
        return None
    return f"https://coverartarchive.org/release/{release_id}/front-250"


def _mapping() -> dict[str, dict[str, str]]:
    """Return effective mapping from hardcoded defaults + optional env override."""
    mapping = dict(HARDCODED_TELEGRAM_MAPPING)

    raw = os.getenv("TELEGRAM_FILE_MAPPING", "").strip()
    if not raw:
        return mapping

    try:
        mapping.update(env_mapping(raw))
        return mapping
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"Invalid TELEGRAM_FILE_MAPPING JSON: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def _recording_search_url(mbid: str) -> str:
    rec = _mb_get(f"recording/{mbid}", {"inc": "artists+releases"})
    title = rec.get("title", "")
    artist = safe_artist_string(rec.get("artist-credit"))

    release_date = ""
    releases = rec.get("releases", [])
    if releases and isinstance(releases[0], dict):
        release_date = str(releases[0].get("date", ""))
    year = release_date[:4] if release_date else None

    query = build_recording_search_query(title=title, artist=artist, year=year)
    return build_telegram_search_url(query)


@app.get("/manifest.json")
def manifest() -> dict[str, Any]:
    return {
        "id": "community.musicbrainz.telegram",
        "version": "0.7.0",
        "name": "MusicBrainz + Telegram",
        "description": "Music catalog from MusicBrainz and Telegram playback links.",
        "resources": ["catalog", "meta", "stream"],
        "types": ["movie"],
        "catalogs": [
            {
                "type": "movie",
                "id": "musicbrainz-popular",
                "name": "Music - Popular",
                "extra": [{"name": "search", "isRequired": False}],
            },
            {
                "type": "movie",
                "id": "musicbrainz-recent",
                "name": "Music - Recent",
                "extra": [{"name": "search", "isRequired": False}],
            },
            {
                "type": "movie",
                "id": "musicbrainz-search",
                "name": "Music - Search",
                "extra": [{"name": "search", "isRequired": False}],
            }
        ],
        "idPrefixes": ["mb:"],
    }


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {
        "status": "ok",
        "telegram_app_credentials": has_telegram_app_credentials(),
        "mapping_entries": len(_mapping()),
        "cache_entries": len(_MB_CACHE),
        "search_hints": len(_SEARCH_HINTS),
    }


def _remember_search_hints(search: str | None, recordings: list[dict[str, Any]]) -> None:
    if not search or not search.strip():
        return

    now = time.time()
    phrase = search.strip()
    for rec in recordings:
        rec_id = rec.get("id")
        if rec_id:
            _SEARCH_HINTS[str(rec_id)] = (now + _SEARCH_HINT_TTL_SECONDS, phrase)


def _search_hint_for_mbid(mbid: str) -> str | None:
    data = _SEARCH_HINTS.get(mbid)
    if not data:
        return None
    if data[0] < time.time():
        _SEARCH_HINTS.pop(mbid, None)
        return None
    return data[1]


def _catalog_query(catalog_id: str, search: str | None) -> str:
    if search and search.strip():
        return search.strip()

    if catalog_id == "musicbrainz-popular":
        # MusicBrainz has no official trending endpoint like TMDB.
        return "tag:pop OR tag:rock"

    if catalog_id == "musicbrainz-recent":
        # Approximate "new releases" by date range search.
        return "date:[2023 TO *]"

    if catalog_id == "musicbrainz-search":
        return "tag:music"

    raise HTTPException(status_code=404, detail="Catalog not found")


@app.get("/catalog/{type}/{catalog_id}.json")
def catalog(type: str, catalog_id: str, search: str | None = None) -> dict[str, Any]:
    if type != "movie":
        raise HTTPException(status_code=404, detail="Catalog not found")

    query = _catalog_query(catalog_id=catalog_id, search=search)

    try:
        data = _mb_get("recording", {"query": query, "limit": SETTINGS.search_limit})
    except requests.RequestException as exc:
        logger.exception("MusicBrainz catalog query failed")
        raise HTTPException(status_code=502, detail=f"MusicBrainz request failed: {exc}") from exc

    recordings = data.get("recordings", [])
    _remember_search_hints(search=search, recordings=recordings)

    metas = []
    for rec in recordings:
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
    try:
        rec = _mb_get(f"recording/{mbid}", {"inc": "artists+releases"})
    except requests.RequestException as exc:
        logger.exception("MusicBrainz meta query failed")
        raise HTTPException(status_code=502, detail=f"MusicBrainz request failed: {exc}") from exc

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

    if entry and "direct_url" in entry:
        return {"streams": [{"title": "Direct URL", "url": entry["direct_url"]}]}

    if entry and "message_url" in entry:
        return {
            "streams": [
                {
                    "title": "Telegram message link",
                    "externalUrl": entry["message_url"],
                }
            ]
        }

    # No direct mapping: prioritize exact user search phrase from catalog search context.
    query_hint = _search_hint_for_mbid(mbid)

    if query_hint:
        return {
            "streams": [
                {
                    "title": "Search phrase on VKMusic bot",
                    "externalUrl": build_telegram_search_url(query_hint),
                },
                {
                    "title": "Send phrase to direct download bot",
                    "externalUrl": build_direct_download_bot_url(query_hint),
                },
            ]
        }

    # Fallback: build query from recording metadata (artist + title + year).
    try:
        metadata_query_url = _recording_search_url(mbid)
    except requests.RequestException:
        metadata_query_url = build_linkfilesbot_url(mbid)

    return {
        "streams": [
            {
                "title": "Search on Telegram (artist/title/year)",
                "externalUrl": metadata_query_url,
            }
        ]
    }
