import asyncio
import os
import re
from collections.abc import Iterable
from typing import Any

URL_RE = re.compile(r"https?://\S+")


def _extract_urls_from_text(text: str | None) -> list[str]:
    if not text:
        return []
    return [m.group(0).rstrip(').,]') for m in URL_RE.finditer(text)]


def _extract_urls_from_message(message: Any) -> list[str]:
    urls: list[str] = []
    urls.extend(_extract_urls_from_text(getattr(message, "raw_text", None)))

    buttons = getattr(message, "buttons", None) or []
    for row in buttons:
        for button in row:
            button_url = getattr(button, "url", None)
            if isinstance(button_url, str) and button_url.startswith(("http://", "https://")):
                urls.append(button_url)

    return urls


def _first_url_from_messages(messages: Iterable[Any]) -> str | None:
    for message in messages:
        urls = _extract_urls_from_message(message)
        if urls:
            return urls[0]
    return None


def _first_non_telegram_url(messages: Iterable[Any]) -> str | None:
    for message in messages:
        for url in _extract_urls_from_message(message):
            if "t.me/" not in url and "telegram.me/" not in url:
                return url
    return None


async def _collect_new_messages(client: Any, peer: str, min_id: int, *, limit: int, wait_seconds: float) -> list[Any]:
    await asyncio.sleep(wait_seconds)
    # Keep only messages sent after our request; sort oldest->newest to preserve bot result order.
    messages = await client.get_messages(peer, limit=limit, min_id=min_id)
    return sorted(messages, key=lambda m: m.id)


async def resolve_direct_url_from_bots(query: str) -> str:
    api_id_raw = os.getenv("TELEGRAM_API_ID", "").strip()
    api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
    session = os.getenv("TELEGRAM_STRING_SESSION", "").strip()

    if not api_id_raw or not api_hash or not session:
        raise RuntimeError("TELEGRAM_API_ID, TELEGRAM_API_HASH and TELEGRAM_STRING_SESSION are required")

    api_id = int(api_id_raw)
    search_bot = os.getenv("VKMUSIC_BOT_USERNAME", "vkmusic_bot").lstrip("@")
    direct_bot = os.getenv("DIRECT_DOWNLOAD_BOT_USERNAME", "LinkFilesBot").lstrip("@")
    wait_seconds = float(os.getenv("MT_PROTO_WAIT_SECONDS", "2.5"))

    from telethon import TelegramClient
    from telethon.sessions import StringSession

    client = TelegramClient(StringSession(session), api_id, api_hash)
    await client.connect()

    try:
        sent_to_search = await client.send_message(search_bot, query)
        search_messages = await _collect_new_messages(
            client,
            search_bot,
            sent_to_search.id,
            limit=20,
            wait_seconds=wait_seconds,
        )

        # Requirement: first bot can return multiple links; choose the first one.
        first_result_link = _first_url_from_messages(search_messages)
        if not first_result_link:
            raise RuntimeError("No result link found in search bot response")

        sent_to_direct = await client.send_message(direct_bot, first_result_link)
        direct_messages = await _collect_new_messages(
            client,
            direct_bot,
            sent_to_direct.id,
            limit=20,
            wait_seconds=wait_seconds,
        )
        direct_url = _first_non_telegram_url(direct_messages) or _first_url_from_messages(direct_messages)

        if not direct_url:
            raise RuntimeError("No direct URL found in direct-download bot response")

        return direct_url
    finally:
        await client.disconnect()
