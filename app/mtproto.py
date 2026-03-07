import asyncio
import os
import re
from collections.abc import Iterable
from typing import Any

URL_RE = re.compile(r"https?://\S+")
START_LINK_RE = re.compile(r"https?://t\.me/(?P<bot>[A-Za-z0-9_]+)\?start=(?P<payload>[A-Za-z0-9_-]+)", re.IGNORECASE)


def _is_telegram_url(url: str) -> bool:
    normalized = url.lower()
    return "t.me/" in normalized or "telegram.me/" in normalized


def _session_path_from_env() -> str:
    return os.path.expanduser(os.getenv("TELEGRAM_SESSION_PATH", "~/telegram_bridge/flix_session").strip())


def _message_text(message: Any) -> str:
    return str(
        getattr(message, "raw_text", None)
        or getattr(message, "text", None)
        or getattr(message, "message", None)
        or ""
    )


def _extract_urls_from_text(text: str | None) -> list[str]:
    if not text:
        return []
    return [m.group(0).rstrip(').,]') for m in URL_RE.finditer(text)]


def _extract_urls_from_message(message: Any) -> list[str]:
    urls: list[str] = []
    text = _message_text(message)
    urls.extend(_extract_urls_from_text(text))

    for entity in getattr(message, "entities", None) or []:
        entity_url = getattr(entity, "url", None)
        if isinstance(entity_url, str) and entity_url.startswith(("http://", "https://")):
            urls.append(entity_url)
            continue

        offset = getattr(entity, "offset", None)
        length = getattr(entity, "length", None)
        if isinstance(offset, int) and isinstance(length, int) and offset >= 0 and length > 0:
            candidate = text[offset : offset + length].strip()
            if candidate.startswith(("http://", "https://")):
                urls.append(candidate)

    buttons = getattr(message, "buttons", None) or []
    for row in buttons:
        for button in row:
            button_url = getattr(button, "url", None)
            if isinstance(button_url, str) and button_url.startswith(("http://", "https://")):
                urls.append(button_url)

    reply_markup = getattr(message, "reply_markup", None)
    for row in getattr(reply_markup, "rows", None) or []:
        for button in getattr(row, "buttons", None) or []:
            button_url = getattr(button, "url", None)
            if isinstance(button_url, str) and button_url.startswith(("http://", "https://")):
                urls.append(button_url)

    deduped: list[str] = []
    seen: set[str] = set()
    for url in urls:
        normalized = str(url).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return deduped


def _extract_start_payload(url: str, *, expected_bot: str | None = None) -> str | None:
    match = START_LINK_RE.search(str(url or "").strip())
    if not match:
        return None

    if expected_bot:
        bot_name = str(match.group("bot") or "").strip().lower()
        if bot_name != str(expected_bot or "").strip().lstrip("@").lower():
            return None

    payload = str(match.group("payload") or "").strip()
    return payload or None


def _first_url_from_messages(messages: Iterable[Any]) -> str | None:
    for message in messages:
        urls = _extract_urls_from_message(message)
        if urls:
            return urls[0]
    return None


def _first_non_telegram_url(messages: Iterable[Any]) -> str | None:
    for message in messages:
        for url in _extract_urls_from_message(message):
            if not _is_telegram_url(url):
                return url
    return None


def _select_search_result_candidate(messages: Iterable[Any], *, search_bot: str) -> str | None:
    start_links: list[str] = []
    for message in messages:
        for url in _extract_urls_from_message(message):
            if _extract_start_payload(url, expected_bot=search_bot):
                start_links.append(url)
                continue
            return url

    if start_links:
        return start_links[0]
    return None


def _first_document_message(messages: Iterable[Any]) -> Any | None:
    for message in messages:
        if getattr(message, "document", None) or getattr(message, "file", None):
            return message
    return None


def _first_result_button_coords(message: Any) -> tuple[int, int] | None:
    rows = getattr(message, "buttons", None) or []
    for row_index, row in enumerate(rows):
        for col_index, button in enumerate(row):
            text = str(getattr(button, "text", "") or "").strip()
            data = getattr(button, "data", None)
            data_text = data.decode("utf-8", errors="ignore") if isinstance(data, (bytes, bytearray)) else str(data or "")
            if text.isdigit() and data_text.startswith("a:"):
                return row_index, col_index
            if re.match(r"^\d+\.", text) and data_text.startswith("download:"):
                return row_index, col_index
    return None


async def _latest_message_id(client: Any, peer: str) -> int:
    messages = await client.get_messages(peer, limit=1)
    if not messages:
        return 0
    return max(0, int(getattr(messages[0], "id", 0) or 0))


async def _collect_new_messages(
    client: Any,
    peer: str,
    min_id: int,
    *,
    limit: int,
    wait_seconds: float,
    stop_when: Any | None = None,
    settle_seconds: float = 0.35,
) -> list[Any]:
    deadline = asyncio.get_running_loop().time() + max(0.0, float(wait_seconds or 0.0))
    poll_seconds = 0.25
    collected: dict[int, Any] = {}
    settle_deadline: float | None = None
    while True:
        messages = await client.get_messages(peer, limit=limit, min_id=min_id)
        for message in messages:
            message_id = int(getattr(message, 'id', 0) or 0)
            if message_id > int(min_id or 0):
                collected[message_id] = message

        ordered = [collected[key] for key in sorted(collected)]
        now = asyncio.get_running_loop().time()
        if stop_when and stop_when(ordered):
            if settle_deadline is None:
                settle_deadline = now + max(0.0, float(settle_seconds or 0.0))
            if now >= settle_deadline:
                return ordered
        else:
            settle_deadline = None

        if now >= deadline:
            return ordered

        sleep_for = min(poll_seconds, max(0.0, deadline - now))
        if settle_deadline is not None:
            sleep_for = min(sleep_for, max(0.0, settle_deadline - now))
        await asyncio.sleep(sleep_for)


async def _authorized_client(api_id: int, api_hash: str, session: str, session_path: str):
    from telethon import TelegramClient

    session_obj: Any = session_path
    if session:
        from telethon.sessions import StringSession

        try:
            session_obj = StringSession(session)
        except Exception as exc:
            raise RuntimeError("TELEGRAM_STRING_SESSION is not a valid Telethon StringSession") from exc

    try:
        client = TelegramClient(session_obj, api_id, api_hash)
        await client.connect()
    except Exception as exc:
        if session_path and not session:
            raise RuntimeError(f"TELEGRAM_SESSION_PATH could not be opened: {session_path}") from exc
        raise

    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError("Telegram client is not authorized; provide a valid TELEGRAM_STRING_SESSION or TELEGRAM_SESSION_PATH")

    return client



def _direct_response_ready(messages: list[Any]) -> bool:
    return bool(_first_non_telegram_url(messages))


def _search_queries(query: str) -> list[str]:
    normalized = " ".join(str(query or "").strip().split())
    if not normalized:
        return []

    variants = [normalized]
    if " - " in normalized:
        left, right = [part.strip() for part in normalized.split(" - ", 1)]
        variants.append(f"{left} {right}")

    cleaned = re.sub(r"[^\w\s]", " ", normalized, flags=re.UNICODE)
    cleaned = " ".join(cleaned.split())
    if cleaned and cleaned not in variants:
        variants.append(cleaned)

    deduped = []
    seen = set()
    for item in variants:
        key = item.casefold()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


async def _resolve_search_result(client: Any, search_bot: str, query: str, wait_seconds: float) -> tuple[str | None, Any | None]:
    sent_to_search = await client.send_message(search_bot, query)
    search_messages = await _collect_new_messages(
        client,
        search_bot,
        sent_to_search.id,
        limit=20,
        wait_seconds=wait_seconds,
        stop_when=lambda messages: bool(
            _select_search_result_candidate(messages, search_bot=search_bot)
            or _first_document_message(messages)
            or any(_first_result_button_coords(message) for message in messages)
        ),
    )

    candidate_url = _select_search_result_candidate(search_messages, search_bot=search_bot)
    if candidate_url:
        return candidate_url, None

    latest_seen_id = max([sent_to_search.id, *[int(getattr(message, "id", 0) or 0) for message in search_messages]], default=sent_to_search.id)
    for message in search_messages:
        coords = _first_result_button_coords(message)
        if not coords:
            continue
        await message.click(*coords)
        followup_messages = await _collect_new_messages(
            client,
            search_bot,
            latest_seen_id,
            limit=10,
            wait_seconds=wait_seconds,
            stop_when=lambda messages: bool(_first_document_message(messages) or _first_non_telegram_url(messages)),
        )
        document_message = _first_document_message(followup_messages)
        if document_message:
            return None, document_message
        followup_url = _first_non_telegram_url(followup_messages)
        if followup_url:
            return followup_url, None

    return None, None


async def resolve_direct_url_from_bots(query: str) -> str:
    api_id_raw = os.getenv("TELEGRAM_API_ID", "").strip()
    api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
    session = os.getenv("TELEGRAM_STRING_SESSION", "").strip()
    session_path = _session_path_from_env()

    if not api_id_raw or not api_hash or not (session or session_path):
        raise RuntimeError("TELEGRAM_API_ID, TELEGRAM_API_HASH and TELEGRAM_STRING_SESSION or TELEGRAM_SESSION_PATH are required")

    api_id = int(api_id_raw)
    search_bot = os.getenv("VKMUSIC_BOT_USERNAME", "MusicDownloaderRobot").lstrip("@")
    direct_bot = os.getenv("DIRECT_DOWNLOAD_BOT_USERNAME", "LinkFilesBot").lstrip("@")
    wait_seconds = float(os.getenv("MT_PROTO_WAIT_SECONDS", "6"))

    client = await _authorized_client(api_id, api_hash, session, session_path)

    try:
        candidate = str(query or "").strip()
        document_message = None
        if not _is_telegram_url(candidate):
            for search_query in _search_queries(candidate):
                candidate, document_message = await _resolve_search_result(client, search_bot, search_query, wait_seconds)
                if candidate or document_message is not None:
                    break
            if not candidate and document_message is None:
                raise RuntimeError("No result link or document found in search bot response")

        before_direct_id = await _latest_message_id(client, direct_bot)
        if document_message is not None:
            await client.forward_messages(direct_bot, document_message)
        else:
            await client.send_message(direct_bot, candidate)

        direct_messages = await _collect_new_messages(
            client,
            direct_bot,
            before_direct_id,
            limit=20,
            wait_seconds=wait_seconds,
            stop_when=_direct_response_ready,
        )
        direct_url = _first_non_telegram_url(direct_messages)

        if not direct_url:
            raise RuntimeError("Direct bot did not return a playable non-Telegram URL")

        return direct_url
    finally:
        await client.disconnect()
