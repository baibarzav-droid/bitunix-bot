import asyncio
from telegram import Bot

BOT_TOKEN = "8361089099:AAEAlU1It-sim1niUecxYaOX24_C5r_PY6Y"
CHAT_ID =  "745887761"

async def main():
    try:
        bot = Bot(token=BOT_TOKEN)
        msg = await bot.send_message(chat_id=CHAT_ID, text="Тест 🚀 Бот работает")
        print("OK")
        print(msg)
    except Exception as e:
        print("ERROR:")
        print(e)

asyncio.run(main())