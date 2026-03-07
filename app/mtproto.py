import asyncio
import os
import re
from typing import Iterable

from telethon import TelegramClient
from telethon.sessions import StringSession

URL_RE = re.compile(r"https?://\S+")


def _extract_first_url(texts: Iterable[str]) -> str | None:
    for text in texts:
        if not text:
            continue
        match = URL_RE.search(text)
        if match:
            return match.group(0)
    return None


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

    client = TelegramClient(StringSession(session), api_id, api_hash)
    await client.connect()

    try:
        await client.send_message(search_bot, query)
        await asyncio.sleep(wait_seconds)
        search_messages = await client.get_messages(search_bot, limit=5)
        search_candidates = [m.raw_text or "" for m in search_messages]

        forwarded_payload = _extract_first_url(search_candidates) or query
        await client.send_message(direct_bot, forwarded_payload)

        await asyncio.sleep(wait_seconds)
        direct_messages = await client.get_messages(direct_bot, limit=10)
        direct_candidates = [m.raw_text or "" for m in direct_messages]
        direct_url = _extract_first_url(direct_candidates)

        if not direct_url:
            raise RuntimeError("No direct URL found in direct-download bot response")

        return direct_url
    finally:
        await client.disconnect()
