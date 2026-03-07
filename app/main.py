import asyncio
import base64
import html
import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from requests.adapters import HTTPAdapter
from urllib.parse import parse_qsl, quote, unquote, urlparse
from urllib.request import Request, build_opener
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
    public_base_url: str = os.getenv("PUBLIC_BASE_URL", "https://flix-music.vercel.app").strip().rstrip("/")
    direct_url_cache_ttl_s: int = int(os.getenv("DIRECT_URL_CACHE_TTL_SECONDS", "1800"))
    image_cache_ttl_s: int = int(os.getenv("IMAGE_CACHE_TTL_SECONDS", "86400"))
    artwork_cache_ttl_s: int = int(os.getenv("ARTWORK_CACHE_TTL_SECONDS", "86400"))


SETTINGS = Settings()

if "contact@" in SETTINGS.user_agent or "example" in SETTINGS.user_agent:
    logger.warning("LASTFM_USER_AGENT should be customized for production deployments.")

app = FastAPI(title="Flix-Music")
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
_DIRECT_URL_CACHE: dict[str, tuple[float, str]] = {}
_IMAGE_CACHE: dict[str, tuple[float, bytes, str]] = {}
_ARTWORK_CACHE: dict[str, tuple[float, str | None]] = {}
HARDCODED_TELEGRAM_MAPPING: dict[str, dict[str, str]] = {}
LOGO_PATH = Path(__file__).resolve().parent / "assets" / "flix-music.png"
LASTFM_PLACEHOLDER_MARKERS = ("2a96cbd8b46e442fc41c2b86b821562f",)
SHORTENER_HOSTS = {"clck.ru", "www.clck.ru"}
BLOCKED_SHORTLINK_TARGET_HOSTS = {
    "share.flocktory.com",
}
BLOCKED_SHORTLINK_TARGET_HOST_FRAGMENTS = (
    "flocktory.com",
)
BLOCKED_SHORTLINK_TARGET_PATH_FRAGMENTS = (
    "/showcaptcha",
    "/exchange/login",
)


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


def _is_placeholder_lastfm_image(url: str | None) -> bool:
    normalized = str(url or "").strip().lower()
    return bool(normalized) and any(marker in normalized for marker in LASTFM_PLACEHOLDER_MARKERS)


def _encode_image_token(url: str) -> str:
    return base64.urlsafe_b64encode(url.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_image_token(token: str) -> str:
    padding = "=" * (-len(token) % 4)
    try:
        value = base64.urlsafe_b64decode((token + padding).encode("ascii")).decode("utf-8")
    except Exception as exc:
        raise HTTPException(status_code=404, detail="Invalid image token") from exc
    if not value.startswith(("http://", "https://")):
        raise HTTPException(status_code=404, detail="Invalid image token")
    return value


def _proxied_image_url(url: str | None) -> str | None:
    normalized = str(url or "").strip()
    if not normalized:
        return None
    return f"{SETTINGS.public_base_url}/image/{_encode_image_token(normalized)}"


def _artwork_cache_key(track_ref: dict[str, str]) -> str:
    return f"{track_ref.get('artist', '').strip().lower()}::{track_ref.get('title', '').strip().lower()}"


def _itunes_artwork(track_ref: dict[str, str]) -> str | None:
    key = _artwork_cache_key(track_ref)
    now = time.time()
    cached = _ARTWORK_CACHE.get(key)
    if cached and cached[0] > now:
        return cached[1]

    artwork = None
    try:
        response = _session.get(
            "https://itunes.apple.com/search",
            params={
                "term": f"{track_ref['artist']} {track_ref['title']}",
                "media": "music",
                "entity": "song",
                "limit": 5,
            },
            headers={"User-Agent": SETTINGS.user_agent},
            timeout=SETTINGS.timeout_s,
        )
        response.raise_for_status()
        payload = response.json()
        for result in payload.get("results", []):
            if not isinstance(result, dict):
                continue
            artwork = str(result.get("artworkUrl100") or result.get("artworkUrl60") or "").strip() or None
            if artwork:
                artwork = artwork.replace("100x100bb", "1200x1200bb").replace("60x60bb", "1200x1200bb")
                break
    except requests.RequestException:
        logger.debug("iTunes artwork lookup failed for %s - %s", track_ref.get("artist"), track_ref.get("title"))

    _ARTWORK_CACHE[key] = (now + SETTINGS.artwork_cache_ttl_s, artwork)
    return artwork


def _poster_url_for_track(track: dict[str, Any]) -> str | None:
    poster = _pick_image(track.get("image"))
    if poster and not _is_placeholder_lastfm_image(poster):
        return _proxied_image_url(poster)

    track_ref = _track_ref_from_catalog_track(track)
    if track_ref:
        artwork = _itunes_artwork(track_ref)
        if artwork:
            return _proxied_image_url(artwork)

    return _proxied_image_url(poster) if poster else None


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


def _encode_play_token(id: str) -> str:
    return base64.urlsafe_b64encode(str(id).encode("utf-8")).decode("ascii").rstrip("=")


def _decode_play_token(token: str) -> str:
    padding = "=" * (-len(token) % 4)
    try:
        value = base64.urlsafe_b64decode((token + padding).encode("ascii")).decode("utf-8")
    except Exception as exc:
        raise HTTPException(status_code=404, detail="Invalid playback token") from exc
    if not value.startswith("lfm:"):
        raise HTTPException(status_code=404, detail="Invalid playback token")
    return value


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


def _catalog_extra_params(extra: str | None) -> dict[str, str]:
    raw = str(extra or '').strip().lstrip('/')
    if not raw:
        return {}
    parsed = dict(parse_qsl(unquote(raw), keep_blank_values=True))
    return {str(k): str(v) for k, v in parsed.items() if v is not None}


def _build_meta_item(track: dict[str, Any], *, id_value: str | None = None) -> dict[str, Any]:
    artist = _track_artist_name(track)
    title = str(track.get("name") or track.get("title") or "Unknown").strip() or "Unknown"
    mbid = str(track.get("mbid") or "").strip()
    poster = _poster_url_for_track(track)
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


def _track_ref_from_catalog_track(track: dict[str, Any]) -> dict[str, str] | None:
    artist = _track_artist_name(track)
    title = str(track.get("name") or track.get("title") or "").strip()
    if not artist or not title:
        return None
    track_ref = {"artist": artist, "title": title}
    mbid = str(track.get("mbid") or "").strip()
    if mbid:
        track_ref["mbid"] = mbid
    return track_ref


def _enrich_track_for_catalog(track: dict[str, Any]) -> dict[str, Any]:
    if _pick_image(track.get("image")):
        return track
    track_ref = _track_ref_from_catalog_track(track)
    if not track_ref:
        return track
    try:
        enriched = _track_info(track_ref)
        if isinstance(enriched, dict):
            return enriched
    except (HTTPException, requests.RequestException):
        logger.debug("Catalog enrichment failed for %s - %s", track_ref.get("artist"), track_ref.get("title"))
    return track


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
        "name": "Flix-Music",
        "description": "Flix-Music: Last.fm discovery catalog with direct playback links.",
        "logo": f"{SETTINGS.public_base_url}/logo.png",
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

    return {"metas": [_build_meta_item(_enrich_track_for_catalog(track)) for track in tracks]}


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


def _cached_or_configured_direct_url(track_ref: dict[str, str], id: str, entry: dict[str, str] | None = None) -> str | None:
    resolved_entry = entry or _find_mapping_entry(track_ref, id)

    if resolved_entry and "direct_url" in resolved_entry:
        configured_url = str(resolved_entry["direct_url"]).strip()
        if _is_telegram_url(configured_url):
            raise HTTPException(status_code=502, detail="Configured direct_url points to Telegram; expected playable media URL")
        return configured_url

    now = time.time()
    cached_direct = _DIRECT_URL_CACHE.get(id)
    if cached_direct and cached_direct[0] > now and not _is_telegram_url(cached_direct[1]):
        return cached_direct[1]

    return None


def _playback_url(id: str) -> str:
    token = quote(_encode_play_token(id), safe="")
    return f"{SETTINGS.public_base_url}/api/music/media?t={token}"


def _should_resolve_shortened_url(url: str) -> bool:
    host = str(urlparse(str(url or "")).netloc or "").strip().lower()
    return host in SHORTENER_HOSTS


def _extract_shortlink_target(url: str) -> str:
    candidate = str(url or "").strip()
    if not candidate:
        return ""

    parsed = urlparse(candidate)
    host = str(parsed.netloc or "").strip().lower()
    if host == "sba.yandex.ru" and parsed.path.startswith("/redirect"):
        redirected = str(dict(parse_qsl(parsed.query, keep_blank_values=True)).get("url") or "").strip()
        return _extract_shortlink_target(redirected)
    return candidate


def _is_blocked_shortlink_target(url: str) -> bool:
    candidate = str(url or "").strip()
    if not candidate:
        return True

    parsed = urlparse(candidate)
    host = str(parsed.netloc or "").strip().lower()
    path = str(parsed.path or "").strip().lower()
    if host in BLOCKED_SHORTLINK_TARGET_HOSTS:
        return True
    if any(fragment in host for fragment in BLOCKED_SHORTLINK_TARGET_HOST_FRAGMENTS):
        return True
    if any(fragment in path for fragment in BLOCKED_SHORTLINK_TARGET_PATH_FRAGMENTS):
        return True
    return False


def _expand_shortlink_with_curl(url: str) -> str:
    candidate = str(url or "").strip()
    if not candidate:
        return ""

    curl_binary = shutil.which("curl") or shutil.which("curl.exe")
    if not curl_binary:
        return ""

    try:
        result = subprocess.run(
            [curl_binary, "-I", "-L", "-s", "-o", os.devnull, "-w", "%{url_effective}", candidate],
            capture_output=True,
            text=True,
            timeout=SETTINGS.timeout_s,
            check=False,
        )
    except Exception:
        return ""

    returncode = getattr(result, "returncode", 1)
    if returncode is None or int(returncode) != 0:
        return ""

    return str(getattr(result, "stdout", "") or "").strip()


def _expand_direct_stream_url(url: str) -> str:
    normalized = str(url or "").strip()
    if not normalized:
        return normalized

    if not _should_resolve_shortened_url(normalized):
        return normalized

    curl_url = str(_expand_shortlink_with_curl(normalized) or "").strip()
    if curl_url:
        curl_url = _extract_shortlink_target(curl_url)
        if curl_url and not _should_resolve_shortened_url(curl_url) and not _is_blocked_shortlink_target(curl_url):
            return curl_url

    opener = build_opener()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "*/*",
    }
    for method in ("HEAD", "GET"):
        try:
            request = Request(normalized, headers=headers, method=method)
            response = opener.open(request, timeout=SETTINGS.timeout_s)
            try:
                final_url = str(response.geturl() or normalized).strip()
            finally:
                try:
                    response.close()
                except Exception:
                    pass
            final_url = _extract_shortlink_target(final_url)
            if final_url and not _should_resolve_shortened_url(final_url) and not _is_blocked_shortlink_target(final_url):
                return final_url
        except Exception:
            continue

    return normalized


def _build_stream_item(url: str, track_ref: dict[str, str]) -> dict[str, Any]:
    label = build_recording_search_query(track_ref.get("title", ""), track_ref.get("artist", ""), track_ref.get("year"))
    return {
        "name": "Direct",
        "title": label,
        "url": url,
        "behaviorHints": {"notWebReady": True},
    }


def _resolve_direct_stream_url(id: str) -> str:
    track_ref = _decode_track_id(id)
    entry = _find_mapping_entry(track_ref, id)
    existing = _cached_or_configured_direct_url(track_ref, id, entry)
    if existing:
        return existing

    if entry and "message_url" in entry:
        query = entry["message_url"]
    else:
        query = build_recording_search_query(track_ref["title"], track_ref["artist"], track_ref.get("year"))

    try:
        direct_url = asyncio.run(resolve_direct_url_from_bots(query))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"MTProto bot chain failed: {exc}") from exc

    direct_url = _expand_direct_stream_url(direct_url)
    _DIRECT_URL_CACHE[id] = (time.time() + SETTINGS.direct_url_cache_ttl_s, direct_url)
    return direct_url


def _stream_payload(type: str, id: str) -> dict[str, Any]:
    if type != "movie" or not id.startswith("lfm:"):
        raise HTTPException(status_code=404, detail="Stream not found")

    track_ref = _decode_track_id(id)
    try:
        direct_url = _resolve_direct_stream_url(id)
    except HTTPException as exc:
        logger.info("Skipping stream for %s - %s because direct resolution failed: %s", track_ref.get("artist"), track_ref.get("title"), exc.detail)
        return {"streams": []}

    return {"streams": [_build_stream_item(direct_url, track_ref)]}


@app.get("/api/music/media", include_in_schema=False)
def api_music_media(t: str) -> RedirectResponse:
    return RedirectResponse(url=_resolve_direct_stream_url(_decode_play_token(t)), status_code=302)


@app.get("/play/{token}/{label}", include_in_schema=False)
def play(token: str, label: str) -> RedirectResponse:
    _ = label
    return RedirectResponse(url=_resolve_direct_stream_url(_decode_play_token(token)), status_code=302)


@app.get("/play/{id}", include_in_schema=False)
def play_legacy(id: str) -> RedirectResponse:
    return RedirectResponse(url=_resolve_direct_stream_url(id), status_code=302)


@app.get("/")
def root() -> RedirectResponse:
    return RedirectResponse(url="/configure", status_code=307)


@app.get("/configure", response_class=HTMLResponse)
def configure() -> str:
    return r"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Flix-Music Configure</title>
  <style>
    :root {
      --bg-top: #07111f;
      --bg-bottom: #10273f;
      --card: rgba(11, 22, 39, 0.86);
      --card-edge: rgba(148, 163, 184, 0.18);
      --text: #f8fafc;
      --muted: #9fb0c7;
      --accent: #5ee7b7;
      --accent-strong: #34d399;
      --accent-dark: #052e2b;
      --secondary: #1d4ed8;
      --secondary-soft: rgba(29, 78, 216, 0.14);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Segoe UI, sans-serif;
      color: var(--text);
      background: radial-gradient(circle at top left, rgba(94, 231, 183, 0.14), transparent 32%), linear-gradient(160deg, var(--bg-top), var(--bg-bottom));
      min-height: 100vh;
    }
    .shell { max-width: 1080px; margin: 0 auto; padding: 40px 20px 56px; }
    .stack { display: grid; gap: 20px; }
    .hero {
      background: linear-gradient(145deg, rgba(8, 19, 35, 0.92), rgba(13, 31, 53, 0.88));
      border: 1px solid var(--card-edge);
      border-radius: 28px;
      padding: 28px;
      box-shadow: 0 24px 70px rgba(0, 0, 0, 0.28);
    }
    .hero-grid { display: grid; gap: 22px; grid-template-columns: minmax(0, 1.2fr) minmax(320px, 0.8fr); align-items: start; }
    .eyebrow { color: var(--accent); font-size: 0.8rem; font-weight: 700; letter-spacing: 0.14em; text-transform: uppercase; }
    h1 { margin: 10px 0 12px; font-size: clamp(2rem, 4vw, 3.3rem); line-height: 0.96; }
    p { line-height: 1.6; margin: 0; }
    .muted { color: var(--muted); }
    .panel, .card {
      background: var(--card);
      border: 1px solid var(--card-edge);
      border-radius: 22px;
      padding: 22px;
      backdrop-filter: blur(10px);
    }
    .label { display: block; margin-bottom: 10px; color: var(--muted); font-size: 0.92rem; }
    input {
      width: 100%;
      padding: 14px 16px;
      border-radius: 16px;
      border: 1px solid rgba(148, 163, 184, 0.24);
      background: rgba(2, 6, 23, 0.78);
      color: var(--text);
      font-size: 1rem;
      outline: none;
    }
    input:focus { border-color: rgba(94, 231, 183, 0.7); box-shadow: 0 0 0 4px rgba(94, 231, 183, 0.14); }
    .actions, .links { display: flex; flex-wrap: wrap; gap: 12px; }
    .actions { margin-top: 14px; }
    .button, button {
      appearance: none;
      border: 0;
      border-radius: 999px;
      padding: 13px 18px;
      font-weight: 700;
      cursor: pointer;
      text-decoration: none;
      transition: transform 140ms ease, opacity 140ms ease, background 140ms ease;
      display: inline-flex;
      align-items: center;
      justify-content: center;
    }
    .button:hover, button:hover { transform: translateY(-1px); }
    .button.primary, button.primary { background: var(--accent); color: var(--accent-dark); }
    .button.secondary, button.secondary { background: var(--secondary-soft); color: #dbeafe; border: 1px solid rgba(147, 197, 253, 0.22); }
    .button.ghost, button.ghost { background: rgba(148, 163, 184, 0.08); color: var(--text); border: 1px solid rgba(148, 163, 184, 0.18); }
    .button[aria-disabled="true"] { opacity: 0.5; pointer-events: none; }
    .output { display: grid; gap: 14px; }
    .manifest-wrap { display: grid; gap: 10px; }
    .manifest-field { width: 100%; font-family: Consolas, monospace; font-size: 0.95rem; }
    .hint { font-size: 0.92rem; color: var(--muted); }
    .status { min-height: 1.3em; color: var(--accent); font-size: 0.92rem; }
    .grid { display: grid; gap: 20px; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); }
    .addon-list { display: grid; gap: 14px; }
    .addon { padding-top: 14px; border-top: 1px solid rgba(148, 163, 184, 0.12); }
    .addon:first-child { padding-top: 0; border-top: 0; }
    .links a { color: #bfdbfe; text-decoration: none; }
    .links a:hover { text-decoration: underline; }
    h2 { margin: 0 0 12px; font-size: 1.08rem; }
    h3 { margin: 0; font-size: 1rem; }
    .pill { display: inline-flex; align-items: center; gap: 8px; padding: 8px 12px; border-radius: 999px; background: rgba(94, 231, 183, 0.12); color: #d1fae5; font-size: 0.88rem; }
    @media (max-width: 860px) { .hero-grid { grid-template-columns: 1fr; } .shell { padding-top: 24px; } }
  </style>
</head>
<body>
  <div class="shell">
    <div class="stack">
      <section class="hero">
        <div class="hero-grid">
          <div class="panel">
            <div class="eyebrow">Personal music addon</div>
            <h1>Flix-Music</h1>
            <p class="muted">Build a personal manifest from a Last.fm username, copy it in one click, or open the direct install flow in Stremio immediately.</p>
          </div>
          <div class="panel output">
            <span class="pill">Stremio-ready manifest</span>
            <label class="label" for="lastfmUser">Last.fm username</label>
            <input id="lastfmUser" placeholder="your-lastfm-username" autocomplete="off" />
            <div class="actions">
              <button id="generateButton" type="button" class="primary">Generate manifest</button>
              <button id="copyButton" type="button" class="ghost">Copy URL</button>
            </div>
            <div class="manifest-wrap">
              <input id="manifestUrl" class="manifest-field" readonly value="" />
              <div class="actions">
                <a id="manifestLink" class="button secondary" href="#" target="_blank" rel="noreferrer" aria-disabled="true">Open manifest</a>
                <a id="installLink" class="button primary" href="#" aria-disabled="true">Install in Stremio</a>
              </div>
            </div>
            <p class="hint">If the username is empty, the default public manifest is generated.</p>
            <div id="status" class="status"></div>
          </div>
        </div>
      </section>
      <div class="grid">
        <div class="card">
          <h2>Community</h2>
          <p class="muted">Join the Discord or support the project on Ko-fi.</p>
          <div class="links">
            <a href="https://discord.gg/GnKRAwwdcQ" target="_blank" rel="noreferrer">Discord</a>
            <a href="https://ko-fi.com/sandortoth" target="_blank" rel="noreferrer">Ko-fi</a>
          </div>
        </div>
        <div class="card">
          <h2>More add-ons</h2>
          <div class="addon-list">
            <div class="addon">
              <h3>Flix Streams</h3>
              <div class="links">
                <a href="https://flixnest.app/flix-streams/configure" target="_blank" rel="noreferrer">Configure</a>
                <a href="https://flixnest.app/flix-streams/manifest.json" target="_blank" rel="noreferrer">Default</a>
              </div>
            </div>
            <div class="addon">
              <h3>Flix feliratok</h3>
              <div class="links">
                <a href="https://flixnest.app/feliratok/configure" target="_blank" rel="noreferrer">Configure</a>
                <a href="https://flixnest.app/feliratok/manifest.json" target="_blank" rel="noreferrer">Default</a>
                <a href="https://github.com/Thsandorh/Feliratok.eu-subs" target="_blank" rel="noreferrer">GitHub</a>
              </div>
            </div>
            <div class="addon">
              <h3>Flix Catalogs</h3>
              <div class="links">
                <a href="https://flixnest.app/flix-catalogs/configure" target="_blank" rel="noreferrer">Configure</a>
                <a href="https://flixnest.app/flix-catalogs/manifest.json" target="_blank" rel="noreferrer">Default</a>
                <a href="https://github.com/Thsandorh/Flix-Catalogs" target="_blank" rel="noreferrer">GitHub</a>
              </div>
            </div>
            <div class="addon">
              <h3>Flix Finder</h3>
              <div class="links">
                <a href="https://flixnest.app/flix-finder/configure" target="_blank" rel="noreferrer">Configure</a>
                <a href="https://flixnest.app/flix-finder/manifest.json" target="_blank" rel="noreferrer">Default</a>
                <a href="https://github.com/Thsandorh/Flix-finder" target="_blank" rel="noreferrer">GitHub</a>
              </div>
            </div>
            <div class="addon">
              <h3>HDMozi</h3>
              <div class="links">
                <a href="https://flixnest.app/hd-mozi/configure" target="_blank" rel="noreferrer">Configure</a>
                <a href="https://flixnest.app/hd-mozi/manifest.json" target="_blank" rel="noreferrer">Default</a>
                <a href="https://github.com/Thsandorh/Hd-mozi-scraper" target="_blank" rel="noreferrer">GitHub</a>
              </div>
            </div>
            <div class="addon">
              <h3>nCore</h3>
              <div class="links">
                <a href="https://flixnest.app/ncore/configure" target="_blank" rel="noreferrer">Configure</a>
                <a href="https://flixnest.app/ncore/manifest.json" target="_blank" rel="noreferrer">Default</a>
                <a href="https://github.com/Thsandorh/nCore-addon" target="_blank" rel="noreferrer">GitHub</a>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
  <script>
    function base64UrlEncode(value) {
      return btoa(unescape(encodeURIComponent(value))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/g, '');
    }
    function buildManifestUrl() {
      const username = document.getElementById('lastfmUser').value.trim();
      if (!username) {
        return window.location.origin + '/manifest.json';
      }
      const token = base64UrlEncode(JSON.stringify({ lastfm_user: username }));
      return window.location.origin + '/c/' + token + '/manifest.json';
    }
    function buildStremioUrl(url) {
      return 'stremio://' + url.replace(/^https?:\/\//, '');
    }
    function setButtonState(element, enabled) {
      element.setAttribute('aria-disabled', enabled ? 'false' : 'true');
    }
    function buildUrl() {
      const url = buildManifestUrl();
      const manifestField = document.getElementById('manifestUrl');
      const manifestLink = document.getElementById('manifestLink');
      const installLink = document.getElementById('installLink');
      manifestField.value = url;
      manifestLink.href = url;
      installLink.href = buildStremioUrl(url);
      setButtonState(manifestLink, true);
      setButtonState(installLink, true);
      document.getElementById('status').textContent = 'Manifest ready.';
    }
    async function copyManifest() {
      const manifestField = document.getElementById('manifestUrl');
      const url = manifestField.value || buildManifestUrl();
      manifestField.value = url;
      try {
        await navigator.clipboard.writeText(url);
        document.getElementById('status').textContent = 'Manifest URL copied.';
      } catch (_error) {
        manifestField.focus();
        manifestField.select();
        document.execCommand('copy');
        document.getElementById('status').textContent = 'Manifest URL selected for copy.';
      }
      buildUrl();
    }
    const usernameField = document.getElementById('lastfmUser');
    const generateButton = document.getElementById('generateButton');
    const copyButton = document.getElementById('copyButton');
    usernameField.addEventListener('input', buildUrl);
    generateButton.addEventListener('click', buildUrl);
    copyButton.addEventListener('click', copyManifest);
    buildUrl();
  </script>
</body>
</html>"""


@app.get("/logo.png", include_in_schema=False)
def logo() -> FileResponse:
    return FileResponse(LOGO_PATH, media_type="image/png", headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.get("/image/{token}", include_in_schema=False)
def image_proxy(token: str) -> Response:
    source_url = _decode_image_token(token)
    now = time.time()
    cached = _IMAGE_CACHE.get(source_url)
    if cached and cached[0] > now:
        return Response(content=cached[1], media_type=cached[2], headers={"Cache-Control": "public, max-age=86400, stale-while-revalidate=604800"})

    try:
        response = _session.get(source_url, headers={"User-Agent": SETTINGS.user_agent, "Accept": "image/*"}, timeout=SETTINGS.timeout_s)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Image fetch failed: {exc}") from exc

    content_type = str(response.headers.get("Content-Type") or "image/jpeg").split(";", 1)[0].strip() or "image/jpeg"
    if not content_type.startswith("image/"):
        raise HTTPException(status_code=502, detail="Upstream did not return an image")

    body = response.content
    _IMAGE_CACHE[source_url] = (now + SETTINGS.image_cache_ttl_s, body, content_type)
    return Response(content=body, media_type=content_type, headers={"Cache-Control": "public, max-age=86400, stale-while-revalidate=604800"})


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


@app.get("/catalog/{type}/{catalog_id}/{extra}.json")
def catalog_with_extra(type: str, catalog_id: str, extra: str, lastfm_user: str | None = None) -> dict[str, Any]:
    params = _catalog_extra_params(extra)
    return _catalog_payload(type, catalog_id, search=params.get("search"), lastfm_user=lastfm_user.strip() if lastfm_user else None)


@app.get("/c/{config}/catalog/{type}/{catalog_id}.json")
def configured_catalog(config: str, type: str, catalog_id: str, search: str | None = None) -> dict[str, Any]:
    cfg = _decode_config(config)
    return _catalog_payload(type, catalog_id, search=search, lastfm_user=cfg.get("lastfm_user"))


@app.get("/c/{config}/catalog/{type}/{catalog_id}/{extra}.json")
def configured_catalog_with_extra(config: str, type: str, catalog_id: str, extra: str) -> dict[str, Any]:
    cfg = _decode_config(config)
    params = _catalog_extra_params(extra)
    return _catalog_payload(type, catalog_id, search=params.get("search"), lastfm_user=cfg.get("lastfm_user"))


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


