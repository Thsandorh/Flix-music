import asyncio
import sys
from types import SimpleNamespace

import app.mtproto as mtproto
from app.mtproto import (
    _extract_start_payload,
    _extract_urls_from_message,
    _extract_urls_from_text,
    _first_non_telegram_url,
    _first_result_button_coords,
    _first_url_from_messages,
    _is_telegram_url,
    _select_search_result_candidate,
    resolve_direct_url_from_bots,
)


class _FakeMessage:
    def __init__(
        self,
        text=None,
        *,
        id=1,
        buttons=None,
        entities=None,
        reply_markup=None,
        file_name=None,
        mime_type=None,
        file_size=0,
        has_document=False,
        on_click=None,
    ):
        self.id = id
        self.raw_text = text
        self.text = text
        self.message = text
        self.buttons = buttons
        self.entities = entities
        self.reply_markup = reply_markup
        self._on_click = on_click
        self.file = None
        self.document = None
        if file_name or mime_type or file_size or has_document:
            self.file = SimpleNamespace(name=file_name, mime_type=mime_type, size=file_size)
            self.document = object()

    async def click(self, row, col):
        if self._on_click is not None:
            return await self._on_click(row, col)
        return None


def _msg(text=None, buttons=None, entities=None, reply_markup=None, id=1):
    return _FakeMessage(text, id=id, buttons=buttons, entities=entities, reply_markup=reply_markup)


def _button(url):
    return SimpleNamespace(url=url)


def _callback_button(text, data):
    return SimpleNamespace(text=text, data=data, url=None)


def test_extract_urls_from_text_multiple():
    urls = _extract_urls_from_text("one https://a.test/x and https://b.test/y")
    assert urls == ["https://a.test/x", "https://b.test/y"]


def test_extract_urls_from_message_reads_buttons():
    message = _msg("no url", buttons=[[_button("https://btn.test/1")]])
    assert _extract_urls_from_message(message) == ["https://btn.test/1"]


def test_extract_urls_from_message_reads_entities_and_reply_markup():
    message = _msg(
        "listen https://inline.test/a",
        entities=[SimpleNamespace(url="https://entity.test/b", offset=0, length=0)],
        reply_markup=SimpleNamespace(rows=[SimpleNamespace(buttons=[_button("https://reply.test/c")])]),
    )
    assert _extract_urls_from_message(message) == [
        "https://inline.test/a",
        "https://entity.test/b",
        "https://reply.test/c",
    ]


def test_first_url_from_messages_keeps_message_order():
    messages = [_msg("none", id=1), _msg("https://first.test", id=2), _msg("https://second.test", id=3)]
    assert _first_url_from_messages(messages) == "https://first.test"


def test_first_non_telegram_url_prefers_playable_link():
    messages = [_msg("https://t.me/abc", id=1), _msg("https://cdn.example/song.mp3", id=2)]
    assert _first_non_telegram_url(messages) == "https://cdn.example/song.mp3"


def test_extract_start_payload_filters_to_expected_bot():
    assert _extract_start_payload("https://t.me/vkmusic_bot?start=abc", expected_bot="vkmusic_bot") == "abc"
    assert _extract_start_payload("https://t.me/other_bot?start=abc", expected_bot="vkmusic_bot") is None


def test_select_search_result_candidate_prefers_message_link_over_search_bot_start_link():
    messages = [
        _msg("https://t.me/vkmusic_bot?start=AAA111", id=1),
        _msg("https://t.me/c/123/456", id=2),
    ]
    assert _select_search_result_candidate(messages, search_bot="vkmusic_bot") == "https://t.me/c/123/456"


def test_first_result_button_coords_selects_numbered_callback_button():
    message = _msg(
        "results",
        buttons=[
            [_callback_button("1", b"a:123:1"), _callback_button("2", b"a:456:1")],
            [_callback_button("??", b"ss:8")],
        ],
    )
    assert _first_result_button_coords(message) == (0, 0)


def test_is_telegram_url():
    assert _is_telegram_url("https://t.me/abc") is True
    assert _is_telegram_url("https://telegram.me/abc") is True
    assert _is_telegram_url("https://cdn.example/song.mp3") is False


class _FakeClient:
    def __init__(self, search_messages=None, search_followups=None, direct_messages=None, authorized=True):
        self.search_messages = list(search_messages or [])
        self.search_followups = list(search_followups or [])
        self.direct_messages = list(direct_messages or [])
        self.sent = []
        self.forwarded = []
        self.authorized = authorized
        self.connected = False
        self.search_clicked = False
        self.direct_activated = False

    async def connect(self):
        self.connected = True
        return None

    async def disconnect(self):
        self.connected = False
        return None

    async def is_user_authorized(self):
        return self.authorized

    async def send_message(self, peer, message):
        self.sent.append((peer, message))
        if peer == 'LinkFilesBot':
            self.direct_activated = True
        return SimpleNamespace(id=len(self.sent) * 100)

    async def forward_messages(self, peer, message):
        self.forwarded.append((peer, getattr(message, 'id', None)))
        if peer == 'LinkFilesBot':
            self.direct_activated = True
        return SimpleNamespace(id=900 + len(self.forwarded))

    async def get_messages(self, peer, limit=20, min_id=0):
        if peer == "vkmusic_bot":
            rows = self.search_messages + (self.search_followups if self.search_clicked else [])
        elif peer == "LinkFilesBot":
            rows = self.direct_messages if self.direct_activated else []
        else:
            rows = []
        return [row for row in rows if int(getattr(row, 'id', 0) or 0) > int(min_id or 0)][:limit]


class _FakeTelegramClientFactory:
    def __init__(self, client, calls):
        self.client = client
        self.calls = calls

    def __call__(self, session_obj, api_id, api_hash):
        self.calls.append((session_obj, api_id, api_hash))
        return self.client


class _FakeStringSession:
    def __init__(self, value):
        self.value = value


async def _no_sleep(_seconds):
    return None


async def _mark_clicked(client, _row, _col):
    client.search_clicked = True
    return SimpleNamespace()


def test_resolve_direct_url_from_bots_prefers_non_bot_result_link(monkeypatch):
    client = _FakeClient(
        search_messages=[
            _msg("https://t.me/vkmusic_bot?start=AAA111", id=101),
            _msg("https://t.me/c/123/456", id=102),
        ],
        direct_messages=[_msg("https://cdn.example/song.mp3", id=201)],
    )
    calls = []
    monkeypatch.setenv("TELEGRAM_API_ID", "123")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.setenv("TELEGRAM_STRING_SESSION", "session")
    monkeypatch.delenv("TELEGRAM_SESSION_PATH", raising=False)
    monkeypatch.setenv("MT_PROTO_WAIT_SECONDS", "0")
    monkeypatch.setattr(mtproto.asyncio, "sleep", _no_sleep)
    monkeypatch.setitem(sys.modules, "telethon", SimpleNamespace(TelegramClient=_FakeTelegramClientFactory(client, calls)))
    monkeypatch.setitem(sys.modules, "telethon.sessions", SimpleNamespace(StringSession=_FakeStringSession))

    result = asyncio.run(resolve_direct_url_from_bots("Metallica Nothing Else Matters"))

    assert result == "https://cdn.example/song.mp3"
    assert client.sent == [
        ("vkmusic_bot", "Metallica Nothing Else Matters"),
        ("LinkFilesBot", "https://t.me/c/123/456"),
    ]
    assert client.forwarded == []
    assert isinstance(calls[0][0], _FakeStringSession)


def test_resolve_direct_url_from_bots_clicks_callback_result_and_forwards_document(monkeypatch):
    client = _FakeClient(
        search_messages=[
            _FakeMessage(
                "results",
                id=101,
                buttons=[[_callback_button("1", b"a:123:1")]],
                on_click=lambda row, col: _mark_clicked(client, row, col),
            )
        ],
        search_followups=[
            _FakeMessage(
                "Find music",
                id=102,
                file_name="song.mp3",
                mime_type="audio/mpeg",
                file_size=123,
                has_document=True,
            )
        ],
        direct_messages=[_msg("Download: https://clck.ru/direct", id=201)],
    )
    calls = []
    monkeypatch.setenv("TELEGRAM_API_ID", "123")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.setenv("TELEGRAM_STRING_SESSION", "session")
    monkeypatch.delenv("TELEGRAM_SESSION_PATH", raising=False)
    monkeypatch.setenv("MT_PROTO_WAIT_SECONDS", "0")
    monkeypatch.setattr(mtproto.asyncio, "sleep", _no_sleep)
    monkeypatch.setitem(sys.modules, "telethon", SimpleNamespace(TelegramClient=_FakeTelegramClientFactory(client, calls)))
    monkeypatch.setitem(sys.modules, "telethon.sessions", SimpleNamespace(StringSession=_FakeStringSession))

    result = asyncio.run(resolve_direct_url_from_bots("Metallica Nothing Else Matters"))

    assert result == "https://clck.ru/direct"
    assert client.sent == [("vkmusic_bot", "Metallica Nothing Else Matters")]
    assert client.forwarded == [("LinkFilesBot", 102)]


def test_resolve_direct_url_from_bots_skips_search_for_message_url(monkeypatch):
    client = _FakeClient(
        direct_messages=[_msg("https://cdn.example/song.mp3", id=201)],
    )
    calls = []
    monkeypatch.setenv("TELEGRAM_API_ID", "123")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.setenv("TELEGRAM_STRING_SESSION", "session")
    monkeypatch.delenv("TELEGRAM_SESSION_PATH", raising=False)
    monkeypatch.setenv("MT_PROTO_WAIT_SECONDS", "0")
    monkeypatch.setattr(mtproto.asyncio, "sleep", _no_sleep)
    monkeypatch.setitem(sys.modules, "telethon", SimpleNamespace(TelegramClient=_FakeTelegramClientFactory(client, calls)))
    monkeypatch.setitem(sys.modules, "telethon.sessions", SimpleNamespace(StringSession=_FakeStringSession))

    result = asyncio.run(resolve_direct_url_from_bots("https://t.me/c/123/456"))

    assert result == "https://cdn.example/song.mp3"
    assert client.sent == [("LinkFilesBot", "https://t.me/c/123/456")]
    assert isinstance(calls[0][0], _FakeStringSession)


def test_resolve_direct_url_from_bots_supports_session_path(monkeypatch):
    client = _FakeClient(
        direct_messages=[_msg("https://cdn.example/song.mp3", id=201)],
    )
    calls = []
    monkeypatch.setenv("TELEGRAM_API_ID", "123")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.delenv("TELEGRAM_STRING_SESSION", raising=False)
    monkeypatch.setenv("TELEGRAM_SESSION_PATH", "~/telegram_bridge/flix_session")
    monkeypatch.setenv("MT_PROTO_WAIT_SECONDS", "0")
    monkeypatch.setattr(mtproto.asyncio, "sleep", _no_sleep)
    monkeypatch.setitem(sys.modules, "telethon", SimpleNamespace(TelegramClient=_FakeTelegramClientFactory(client, calls)))

    result = asyncio.run(resolve_direct_url_from_bots("https://t.me/c/123/456"))

    assert result == "https://cdn.example/song.mp3"
    assert calls[0][0].endswith("telegram_bridge/flix_session")


def test_resolve_direct_url_from_bots_reports_invalid_string_session(monkeypatch):
    class _BadStringSession:
        def __init__(self, _value):
            raise ValueError("bad session")

    monkeypatch.setenv("TELEGRAM_API_ID", "123")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.setenv("TELEGRAM_STRING_SESSION", "not-a-string-session")
    monkeypatch.delenv("TELEGRAM_SESSION_PATH", raising=False)
    monkeypatch.setitem(sys.modules, "telethon", SimpleNamespace(TelegramClient=_FakeTelegramClientFactory(_FakeClient(), [])))
    monkeypatch.setitem(sys.modules, "telethon.sessions", SimpleNamespace(StringSession=_BadStringSession))

    try:
        asyncio.run(resolve_direct_url_from_bots("query"))
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert "TELEGRAM_STRING_SESSION" in str(exc)


def test_resolve_direct_url_from_bots_reports_unopenable_default_session_path(monkeypatch):
    monkeypatch.setenv("TELEGRAM_API_ID", "123")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.delenv("TELEGRAM_STRING_SESSION", raising=False)
    monkeypatch.delenv("TELEGRAM_SESSION_PATH", raising=False)

    try:
        asyncio.run(resolve_direct_url_from_bots("query"))
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert "TELEGRAM_SESSION_PATH could not be opened" in str(exc)


def test_resolve_direct_url_from_bots_reports_unauthorized_client(monkeypatch):
    client = _FakeClient(authorized=False)
    calls = []
    monkeypatch.setenv("TELEGRAM_API_ID", "123")
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash")
    monkeypatch.delenv("TELEGRAM_STRING_SESSION", raising=False)
    monkeypatch.setenv("TELEGRAM_SESSION_PATH", "~/telegram_bridge/flix_session")
    monkeypatch.setitem(sys.modules, "telethon", SimpleNamespace(TelegramClient=_FakeTelegramClientFactory(client, calls)))

    try:
        asyncio.run(resolve_direct_url_from_bots("query"))
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert "not authorized" in str(exc)
