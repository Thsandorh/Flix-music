import json
import os
from typing import Any


def env_mapping(raw: str | None = None) -> dict[str, dict[str, str]]:
    """Parse TELEGRAM_FILE_MAPPING into normalized structure.

    Accepted input shapes:
    - {"<mbid>": "<telegram_file_id_or_direct_url>"}
    - {"<mbid>": {"file_id": "...", "direct_url": "..."}}
    """
    source = raw if raw is not None else os.getenv("TELEGRAM_FILE_MAPPING", "{}")
    parsed = json.loads(source)
    if not isinstance(parsed, dict):
        raise ValueError("TELEGRAM_FILE_MAPPING must be a JSON object")

    normalized: dict[str, dict[str, str]] = {}
    for key, value in parsed.items():
        mbid = str(key)

        if isinstance(value, str):
            if value.startswith("http://") or value.startswith("https://"):
                normalized[mbid] = {"direct_url": value}
            else:
                normalized[mbid] = {"file_id": value}
            continue

        if isinstance(value, dict):
            item: dict[str, str] = {}
            if "file_id" in value and value["file_id"]:
                item["file_id"] = str(value["file_id"])
            if "direct_url" in value and value["direct_url"]:
                item["direct_url"] = str(value["direct_url"])

            if not item:
                raise ValueError(f"Invalid mapping for '{mbid}': expected file_id or direct_url")

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


def safe_artist_string(artist_credit: list[Any] | None) -> str:
    if not artist_credit:
        return ""
    return ", ".join(
        item["name"]
        for item in artist_credit
        if isinstance(item, dict) and item.get("name")
    )
