"""Generate Telethon session string via QR code login."""
import asyncio
import qrcode
from telethon import TelegramClient
from telethon.sessions import StringSession

API_ID = 30180026
API_HASH = "1e88e512301f5328e056580d39bd2a64"


async def main():
    print("=== Авторизация через QR-код ===")
    print()

    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()

    qr_login = await client.qr_login()

    print("Отсканируй QR-код телефоном:")
    print("  Telegram → Настройки → Устройства → Подключить устройство")
    print()

    # Show QR code in terminal
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L)
    qr.add_data(qr_login.url)
    qr.print_ascii(invert=True)

    print()
    print("Жду сканирования...")

    try:
        await qr_login.wait(timeout=60)
    except asyncio.TimeoutError:
        print("Таймаут — попробуй ещё раз")
        await client.disconnect()
        return

    session_string = client.session.save()
    print()
    print("=== ГОТОВО! Скопируй эту строку и отправь мне: ===")
    print()
    print(session_string)
    print()

    await client.disconnect()


asyncio.run(main())
