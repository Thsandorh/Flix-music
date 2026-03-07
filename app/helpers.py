import json
import os
from typing import Any
from urllib.parse import quote_plus


def env_mapping(raw: str | None = None) -> dict[str, dict[str, str]]:
    """Parse TELEGRAM_FILE_MAPPING into normalized structure.

    Accepted input shapes:
    - {"<mbid>": "<direct_url_or_message_url>"}
    - {"<mbid>": {"direct_url": "...", "message_url": "..."}}
    """
    source = raw if raw is not None else os.getenv("TELEGRAM_FILE_MAPPING", "{}")
    parsed = json.loads(source)
    if not isinstance(parsed, dict):
        raise ValueError("TELEGRAM_FILE_MAPPING must be a JSON object")

    normalized: dict[str, dict[str, str]] = {}
    for key, value in parsed.items():
        mbid = str(key)

        if isinstance(value, str):
            if value.startswith(("http://", "https://")):
                if "t.me/" in value:
                    normalized[mbid] = {"message_url": value}
                else:
                    normalized[mbid] = {"direct_url": value}
                continue
            raise ValueError(f"Invalid mapping for '{mbid}': string value must be a URL")

        if isinstance(value, dict):
            item: dict[str, str] = {}
            if "direct_url" in value and value["direct_url"]:
                item["direct_url"] = str(value["direct_url"])
            if "message_url" in value and value["message_url"]:
                item["message_url"] = str(value["message_url"])

            if not item:
                raise ValueError(f"Invalid mapping for '{mbid}': expected direct_url or message_url")

            normalized[mbid] = item
            continue

        raise ValueError(f"Invalid mapping for '{mbid}': unsupported value type")

    return normalized


def build_linkfilesbot_url(recording_id: str, template: str | None = None) -> str:
    pattern = template if template is not None else os.getenv(
        "LINKFILESBOT_URL_TEMPLATE",
        "https://t.me/LinkFilesBot?start={recording_id}",
    )
    return pattern.format(recording_id=recording_id)


def build_telegram_search_url(query: str, template: str | None = None) -> str:
    """Build external Telegram search URL with URL-encoded query.

    Supported placeholders in template:
    - {query}: original query
    - {query_encoded}: url-encoded query
    """
    pattern = template if template is not None else os.getenv(
        "TELEGRAM_SEARCH_URL_TEMPLATE",
        "https://t.me/vkmusic_bot?start={query_encoded}",
    )
    return pattern.format(query=query, query_encoded=quote_plus(query))




def build_direct_download_bot_url(query: str, template: str | None = None) -> str:
    """Build direct-download bot URL from a plain text query."""
    pattern = template if template is not None else os.getenv(
        "DIRECT_DOWNLOAD_BOT_URL_TEMPLATE",
        "https://t.me/LinkFilesBot?start={query_encoded}",
    )
    return pattern.format(query=query, query_encoded=quote_plus(query))


def has_telegram_app_credentials() -> bool:
    """True when my.telegram.org app credentials are configured."""
    return bool(os.getenv("TELEGRAM_API_ID")) and bool(os.getenv("TELEGRAM_API_HASH"))


def safe_artist_string(artist_credit: list[Any] | None) -> str:
    if not artist_credit:
        return ""
    return ", ".join(
        item["name"]
        for item in artist_credit
        if isinstance(item, dict) and item.get("name")
    )


def build_recording_search_query(title: str, artist: str, year: str | None = None) -> str:
    """Build human-readable Telegram search query: 'Artist - Title (Year)' style."""
    base = f"{artist} - {title}".strip(" -")
    if year:
        return f"{base} {year}".strip()
    return base
