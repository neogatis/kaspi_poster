"""Start web dashboard and Telegram bot in the same Railway instance and volume."""
import asyncio
import main as bot
import panel
from telegram.ext import CommandHandler

if __name__ == "__main__":
    bot.acquire_local_instance_lock()
    panel.start_server()
    app = bot.build_app()
    app.add_handler(CommandHandler("panel", panel.cmd_panel))
    app.add_handler(CommandHandler("stock", panel.cmd_stock))
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    print("Kaspi Profit Bot + secured inventory dashboard", flush=True)
    app.run_polling(drop_pending_updates=False, bootstrap_retries=bot.TELEGRAM_BOOTSTRAP_RETRIES)
