import base64
import html
import json
import logging
import os
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app.helpers import build_recording_search_query, env_mapping, has_telegram_app_credentials
from app.mtproto import _is_telegram_url, resolve_direct_url_from_bots

logger = logging.getLogger("flix_music")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))


@dataclass(frozen=True)
class Settings:
    lastfm_base: str = os.getenv("LASTFM_BASE", "https://ws.audioscrobbler.com/2.0/")
    lastfm_api_key: str = os.getenv("LASTFM_API_KEY", "").strip()
    user_agent: str = os.getenv("LASTFM_USER_AGENT", "FlixMusicStremioAddon/0.9 (contact@example.com)")
    search_limit: int = int(os.getenv("LASTFM_SEARCH_LIMIT", "20"))
    timeout_s: int = int(os.getenv("HTTP_TIMEOUT_SECONDS", "15"))
    cache_ttl_s: int = int(os.getenv("LASTFM_CACHE_TTL_SECONDS", "300"))
    trending_country: str = os.getenv("LASTFM_TRENDING_COUNTRY", "united states")


SETTINGS = Settings()

if "contact@" in SETTINGS.user_agent or "example" in SETTINGS.user_agent:
    logger.warning("LASTFM_USER_AGENT should be customized for production deployments.")

app = FastAPI(title="Stremio Last.fm + Telegram Addon")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

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

_LASTFM_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
HARDCODED_TELEGRAM_MAPPING: dict[str, dict[str, str]] = {}


def _cache_key(params: dict[str, Any]) -> str:
    ordered = "&".join(f"{k}={params[k]}" for k in sorted(params))
    return ordered


def _lastfm_get(params: dict[str, Any]) -> dict[str, Any]:
    if not SETTINGS.lastfm_api_key:
        raise HTTPException(status_code=500, detail="LASTFM_API_KEY is not configured")

    req_params = {**params, "api_key": SETTINGS.lastfm_api_key, "format": "json"}
    key = _cache_key(req_params)
    now = time.time()

    cached = _LASTFM_CACHE.get(key)
    if cached and cached[0] > now:
        return cached[1]

    response = _session.get(
        SETTINGS.lastfm_base,
        params=req_params,
        headers={"User-Agent": SETTINGS.user_agent},
        timeout=SETTINGS.timeout_s,
    )
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, dict) and payload.get("error"):
        raise HTTPException(status_code=502, detail=f"Last.fm API error: {payload.get('message') or payload.get('error')}")
    _LASTFM_CACHE[key] = (now + SETTINGS.cache_ttl_s, payload)
    return payload


def _track_artist_name(track: dict[str, Any]) -> str:
    artist = track.get("artist")
    if isinstance(artist, dict):
        return str(artist.get("name") or artist.get("#text") or "").strip()
    return str(artist or "").strip()


def _pick_image(images: Any) -> str | None:
    if not isinstance(images, list):
        return None
    preferred_sizes = ["extralarge", "large", "medium", "small"]
    by_size: dict[str, str] = {}
    for image in images:
        if not isinstance(image, dict):
            continue
        url = str(image.get("#text") or "").strip()
        size = str(image.get("size") or "").strip().lower()
        if url:
            by_size[size] = url
    for size in preferred_sizes:
        if by_size.get(size):
            return by_size[size]
    return next(iter(by_size.values()), None)


def _runtime_from_ms(value: Any) -> str:
    try:
        total_seconds = int(int(value) / 1000)
    except Exception:
        return ""
    if total_seconds <= 0:
        return ""
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes}m {seconds:02d}s"


def _year_from_text(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return str(parsedate_to_datetime(text).year)
    except Exception:
        pass
    for chunk in text.split():
        if len(chunk) == 4 and chunk.isdigit():
            return chunk
    return None


def _normalize_tags(track: dict[str, Any]) -> list[str]:
    tags = track.get("toptags", {}).get("tag", [])
    if isinstance(tags, dict):
        tags = [tags]
    names: list[str] = []
    for tag in tags:
        if isinstance(tag, dict) and tag.get("name"):
            names.append(str(tag["name"]))
    return names[:5]


def _build_track_id(*, artist: str, title: str, mbid: str = "", year: str | None = None) -> str:
    payload = {"artist": artist.strip(), "title": title.strip()}
    if mbid:
        payload["mbid"] = mbid.strip()
    if year:
        payload["year"] = str(year).strip()
    encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("ascii").rstrip("=")
    return f"lfm:{encoded}"


def _decode_track_id(value: str) -> dict[str, str]:
    if not value.startswith("lfm:"):
        raise HTTPException(status_code=404, detail="Invalid Last.fm track id")
    raw = value[4:]
    padding = "=" * (-len(raw) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode((raw + padding).encode("ascii")).decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=404, detail="Invalid Last.fm track id") from exc
    if not isinstance(payload, dict) or not payload.get("artist") or not payload.get("title"):
        raise HTTPException(status_code=404, detail="Invalid Last.fm track id")
    return {k: str(v) for k, v in payload.items() if v is not None}


def _encode_config(lastfm_user: str | None = None) -> str:
    payload: dict[str, str] = {}
    if lastfm_user and lastfm_user.strip():
        payload["lastfm_user"] = lastfm_user.strip()
    encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("ascii").rstrip("=")
    return encoded or "e30"


def _decode_config(token: str | None) -> dict[str, str]:
    if not token:
        return {}
    padding = "=" * (-len(token) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode((token + padding).encode("ascii")).decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=404, detail="Invalid config token") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=404, detail="Invalid config token")
    return {str(k): str(v) for k, v in payload.items() if v is not None}


def _configured_manifest_url(token: str) -> str:
    return f"/c/{token}/manifest.json"


def _build_meta_item(track: dict[str, Any], *, id_value: str | None = None) -> dict[str, Any]:
    artist = _track_artist_name(track)
    title = str(track.get("name") or track.get("title") or "Unknown").strip() or "Unknown"
    mbid = str(track.get("mbid") or "").strip()
    poster = _pick_image(track.get("image"))
    album = track.get("album") if isinstance(track.get("album"), dict) else {}
    album_title = str(album.get("title") or album.get("#text") or "").strip()
    year = _year_from_text(album.get("published") or track.get("wiki", {}).get("published") or "")
    tags = _normalize_tags(track)

    description_parts = []
    if artist:
        description_parts.append(f"Artist: {artist}")
    if album_title:
        description_parts.append(f"Album: {album_title}")
    listeners = str(track.get("listeners") or "").strip()
    if listeners:
        description_parts.append(f"Listeners: {listeners}")

    track_id = id_value or _build_track_id(artist=artist, title=title, mbid=mbid, year=year)
    item: dict[str, Any] = {
        "id": track_id,
        "type": "movie",
        "name": title,
        "description": "\n".join(description_parts),
    }
    if poster:
        item["poster"] = poster
        item["background"] = poster
    if artist:
        item["cast"] = [artist]
    runtime = _runtime_from_ms(track.get("duration"))
    if runtime:
        item["runtime"] = runtime
    if tags:
        item["genres"] = tags
    if year:
        item["releaseInfo"] = year
    return item


def _mapping() -> dict[str, dict[str, str]]:
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


def _find_mapping_entry(track_ref: dict[str, str], full_id: str) -> dict[str, str] | None:
    mapping = _mapping()
    for key in (track_ref.get("mbid", ""), full_id, f"{track_ref.get('artist', '')} - {track_ref.get('title', '')}"):
        normalized = str(key or "").strip()
        if normalized and normalized in mapping:
            return mapping[normalized]
    return None


def _normalize_track_list(items: Any) -> list[dict[str, Any]]:
    if isinstance(items, dict):
        return [items]
    return [item for item in items or [] if isinstance(item, dict)]


def _search_tracks(query: str) -> list[dict[str, Any]]:
    payload = _lastfm_get({"method": "track.search", "track": query, "limit": SETTINGS.search_limit})
    return _normalize_track_list(payload.get("results", {}).get("trackmatches", {}).get("track", []))


def _top_tracks() -> list[dict[str, Any]]:
    payload = _lastfm_get({"method": "chart.gettoptracks", "limit": SETTINGS.search_limit})
    return _normalize_track_list(payload.get("tracks", {}).get("track", []))


def _trending_tracks() -> list[dict[str, Any]]:
    payload = _lastfm_get({
        "method": "geo.gettoptracks",
        "country": SETTINGS.trending_country,
        "limit": SETTINGS.search_limit,
    })
    return _normalize_track_list(payload.get("tracks", {}).get("track", []))


def _user_recent_tracks(username: str) -> list[dict[str, Any]]:
    payload = _lastfm_get({"method": "user.getrecenttracks", "user": username, "limit": SETTINGS.search_limit, "extended": 1})
    tracks = _normalize_track_list(payload.get("recenttracks", {}).get("track", []))
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for track in tracks:
        key = (_track_artist_name(track).lower(), str(track.get("name") or "").strip().lower())
        if not key[0] or not key[1] or key in seen:
            continue
        seen.add(key)
        deduped.append(track)
    return deduped


def _user_loved_tracks(username: str) -> list[dict[str, Any]]:
    payload = _lastfm_get({"method": "user.getlovedtracks", "user": username, "limit": SETTINGS.search_limit})
    return _normalize_track_list(payload.get("lovedtracks", {}).get("track", []))


def _user_top_tracks(username: str) -> list[dict[str, Any]]:
    payload = _lastfm_get({"method": "user.gettoptracks", "user": username, "limit": SETTINGS.search_limit, "period": "overall"})
    return _normalize_track_list(payload.get("toptracks", {}).get("track", []))


def _track_info(track_ref: dict[str, str]) -> dict[str, Any]:
    params: dict[str, Any] = {"method": "track.getInfo", "autocorrect": 1}
    if track_ref.get("mbid"):
        params["mbid"] = track_ref["mbid"]
    else:
        params["artist"] = track_ref["artist"]
        params["track"] = track_ref["title"]
    payload = _lastfm_get(params)
    track = payload.get("track")
    if not isinstance(track, dict):
        raise HTTPException(status_code=404, detail="Track not found")
    return track


def _manifest_payload(lastfm_user: str | None = None) -> dict[str, Any]:
    catalogs = [
        {
            "type": "movie",
            "id": "lastfm-top",
            "name": "Music - Top",
            "extra": [{"name": "search", "isRequired": False}],
        },
        {
            "type": "movie",
            "id": "lastfm-trending",
            "name": "Music - Trending",
            "extra": [{"name": "search", "isRequired": False}],
        },
        {
            "type": "movie",
            "id": "lastfm-search",
            "name": "Music - Search",
            "extra": [{"name": "search", "isRequired": False}],
        },
    ]
    if lastfm_user:
        safe_user = lastfm_user.strip()
        catalogs.extend(
            [
                {"type": "movie", "id": "lastfm-user-loved", "name": f"{safe_user} - Loved"},
                {"type": "movie", "id": "lastfm-user-recent", "name": f"{safe_user} - Recent"},
                {"type": "movie", "id": "lastfm-user-top", "name": f"{safe_user} - Top Tracks"},
            ]
        )
    return {
        "id": "community.lastfm.telegram",
        "version": "1.0.0",
        "name": "Last.fm + Telegram",
        "description": "Last.fm discovery catalog with Telegram playback links.",
        "resources": ["catalog", "meta", "stream"],
        "types": ["movie"],
        "catalogs": catalogs,
        "idPrefixes": ["lfm:"],
        "behaviorHints": {"configurable": True},
    }


def _health_payload() -> dict[str, Any]:
    return {
        "status": "ok",
        "lastfm_ready": bool(SETTINGS.lastfm_api_key),
        "telegram_app_credentials": has_telegram_app_credentials(),
        "mapping_entries": len(_mapping()),
        "cache_entries": len(_LASTFM_CACHE),
        "mtproto_ready": bool(os.getenv("TELEGRAM_STRING_SESSION")) or bool(os.getenv("TELEGRAM_SESSION_PATH")),
    }


def _catalog_payload(type: str, catalog_id: str, *, search: str | None = None, lastfm_user: str | None = None) -> dict[str, Any]:
    if type != "movie":
        raise HTTPException(status_code=404, detail="Catalog not found")

    try:
        if search and search.strip():
            tracks = _search_tracks(search.strip())
        elif catalog_id == "lastfm-top":
            tracks = _top_tracks()
        elif catalog_id == "lastfm-trending":
            tracks = _trending_tracks()
        elif catalog_id == "lastfm-search":
            tracks = _top_tracks()
        elif catalog_id == "lastfm-user-loved" and lastfm_user:
            tracks = _user_loved_tracks(lastfm_user)
        elif catalog_id == "lastfm-user-recent" and lastfm_user:
            tracks = _user_recent_tracks(lastfm_user)
        elif catalog_id == "lastfm-user-top" and lastfm_user:
            tracks = _user_top_tracks(lastfm_user)
        else:
            raise HTTPException(status_code=404, detail="Catalog not found")
    except requests.RequestException as exc:
        logger.exception("Last.fm catalog query failed")
        raise HTTPException(status_code=502, detail=f"Last.fm request failed: {exc}") from exc

    return {"metas": [_build_meta_item(track) for track in tracks]}


def _meta_payload(type: str, id: str) -> dict[str, Any]:
    if type != "movie" or not id.startswith("lfm:"):
        raise HTTPException(status_code=404, detail="Meta not found")

    track_ref = _decode_track_id(id)
    try:
        track = _track_info(track_ref)
    except HTTPException:
        fallback_name = build_recording_search_query(track_ref["title"], track_ref["artist"], track_ref.get("year"))
        return {
            "meta": {
                "id": id,
                "type": "movie",
                "name": fallback_name,
                "description": "Metadata temporarily unavailable from Last.fm.",
            }
        }
    except requests.RequestException:
        logger.exception("Last.fm meta query failed")
        fallback_name = build_recording_search_query(track_ref["title"], track_ref["artist"], track_ref.get("year"))
        return {
            "meta": {
                "id": id,
                "type": "movie",
                "name": fallback_name,
                "description": "Metadata temporarily unavailable from Last.fm.",
            }
        }

    return {"meta": _build_meta_item(track, id_value=id)}


def _stream_payload(type: str, id: str) -> dict[str, Any]:
    if type != "movie" or not id.startswith("lfm:"):
        raise HTTPException(status_code=404, detail="Stream not found")

    track_ref = _decode_track_id(id)
    entry = _find_mapping_entry(track_ref, id)

    if entry and "direct_url" in entry:
        configured_url = str(entry["direct_url"]).strip()
        if _is_telegram_url(configured_url):
            raise HTTPException(status_code=502, detail="Configured direct_url points to Telegram; expected playable media URL")
        return {"streams": [{"title": "Direct URL", "url": configured_url}]}

    if entry and "message_url" in entry:
        query = entry["message_url"]
    else:
        query = build_recording_search_query(track_ref["title"], track_ref["artist"], track_ref.get("year"))

    try:
        direct_url = asyncio.run(resolve_direct_url_from_bots(query))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"MTProto bot chain failed: {exc}") from exc

    return {
        "streams": [
            {
                "title": "MTProto resolved direct stream",
                "url": direct_url,
            }
        ]
    }


@app.get("/")
def root() -> RedirectResponse:
    return RedirectResponse(url="/configure", status_code=307)


@app.get("/configure", response_class=HTMLResponse)
def configure() -> str:
    return r"""<!doctype html>
<html>
<head>
  <meta charset=\"utf-8\">
  <title>Flix-music Configure</title>
  <style>
    body { font-family: Segoe UI, sans-serif; max-width: 760px; margin: 40px auto; padding: 0 20px; background: #0f172a; color: #e5e7eb; }
    .card { background: #111827; border: 1px solid #374151; border-radius: 16px; padding: 24px; }
    input { width: 100%; padding: 12px; border-radius: 10px; border: 1px solid #4b5563; background: #030712; color: #fff; }
    button { margin-top: 12px; padding: 12px 16px; border: 0; border-radius: 10px; background: #22c55e; color: #052e16; font-weight: 700; cursor: pointer; }
    a { color: #93c5fd; word-break: break-all; }
    .muted { color: #9ca3af; }
  </style>
</head>
<body>
  <div class=\"card\">
    <h1>Flix-music configuration</h1>
    <p class=\"muted\">Enter a Last.fm username to enable personal Loved, Recent and Top Tracks catalogs.</p>
    <label for=\"lastfmUser\">Last.fm username</label>
    <input id=\"lastfmUser\" placeholder=\"your-lastfm-username\" />
    <button type=\"button\" onclick=\"buildUrl()\">Generate manifest URL</button>
    <p id=\"out\" style=\"margin-top:16px\"></p>
  </div>
  <script>
    function base64UrlEncode(value) {
      return btoa(unescape(encodeURIComponent(value))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/g, '');
    }
    function buildUrl() {
      const username = document.getElementById('lastfmUser').value.trim();
      const payload = username ? { lastfm_user: username } : {};
      const token = base64UrlEncode(JSON.stringify(payload));
      const url = window.location.origin + '/c/' + token + '/manifest.json';
      document.getElementById('out').innerHTML = 'Configured manifest: <a href="' + url + '">' + url + '</a>';
    }
  </script>
</body>
</html>"""


@app.get("/manifest.json")
def manifest(lastfm_user: str | None = None) -> dict[str, Any]:
    return _manifest_payload(lastfm_user=lastfm_user.strip() if lastfm_user else None)


@app.get("/c/{config}/manifest.json")
def configured_manifest(config: str) -> dict[str, Any]:
    cfg = _decode_config(config)
    return _manifest_payload(lastfm_user=cfg.get("lastfm_user"))


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return _health_payload()


@app.get("/catalog/{type}/{catalog_id}.json")
def catalog(type: str, catalog_id: str, search: str | None = None, lastfm_user: str | None = None) -> dict[str, Any]:
    return _catalog_payload(type, catalog_id, search=search, lastfm_user=lastfm_user.strip() if lastfm_user else None)


@app.get("/c/{config}/catalog/{type}/{catalog_id}.json")
def configured_catalog(config: str, type: str, catalog_id: str, search: str | None = None) -> dict[str, Any]:
    cfg = _decode_config(config)
    return _catalog_payload(type, catalog_id, search=search, lastfm_user=cfg.get("lastfm_user"))


@app.get("/meta/{type}/{id}.json")
def meta(type: str, id: str) -> dict[str, Any]:
    return _meta_payload(type, id)


@app.get("/c/{config}/meta/{type}/{id}.json")
def configured_meta(config: str, type: str, id: str) -> dict[str, Any]:
    _decode_config(config)
    return _meta_payload(type, id)


@app.get("/stream/{type}/{id}.json")
def stream(type: str, id: str) -> dict[str, Any]:
    return _stream_payload(type, id)


@app.get("/c/{config}/stream/{type}/{id}.json")
def configured_stream(config: str, type: str, id: str) -> dict[str, Any]:
    _decode_config(config)
    return _stream_payload(type, id)
