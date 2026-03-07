import argparse
import asyncio
import os
from getpass import getpass

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession


def _prompt(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or default


async def _generate_session(api_id: int, api_hash: str, phone: str, code: str | None, password: str | None) -> str:
    async with TelegramClient(StringSession(), api_id, api_hash) as client:
        await client.send_code_request(phone)
        login_code = code or _prompt("Telegram login code")
        try:
            await client.sign_in(phone=phone, code=login_code)
        except SessionPasswordNeededError:
            login_password = password or getpass("Telegram 2FA password: ")
            await client.sign_in(password=login_password)
        return client.session.save()


async def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a Telethon TELEGRAM_STRING_SESSION")
    parser.add_argument("--api-id", type=int, default=int(os.getenv("TELEGRAM_API_ID", "0") or 0))
    parser.add_argument("--api-hash", default=os.getenv("TELEGRAM_API_HASH", ""))
    parser.add_argument("--phone", default=os.getenv("TELEGRAM_PHONE", ""))
    parser.add_argument("--code", default=os.getenv("TELEGRAM_LOGIN_CODE", ""))
    parser.add_argument("--password", default=os.getenv("TELEGRAM_2FA_PASSWORD", ""))
    args = parser.parse_args()

    api_id = int(args.api_id or 0)
    api_hash = str(args.api_hash or "").strip()
    phone = str(args.phone or "").strip() or _prompt("Telegram phone", default="+")
    code = str(args.code or "").strip() or None
    password = str(args.password or "").strip() or None

    if api_id <= 0 or not api_hash:
        raise SystemExit("TELEGRAM_API_ID and TELEGRAM_API_HASH are required")

    session = await _generate_session(api_id=api_id, api_hash=api_hash, phone=phone, code=code, password=password)
    print("\nNEW TELEGRAM_SESSION_STRING:\n")
    print(session)


if __name__ == "__main__":
    asyncio.run(main())
