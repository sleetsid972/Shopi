import asyncio
import io
import logging
from pathlib import Path
from time import monotonic
from typing import List

from telegram import InputFile, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from checker import ProxyManager, check_shopify_store
from config import ADMIN_ID, BOT_TOKEN
from database import add_user, count_users, init_db, is_authorized, remove_user

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
proxy_manager = ProxyManager(BASE_DIR / "proxies.txt")
bot_stats = {
    "single_checks": 0,
    "mass_checks": 0,
    "stores_checked": 0,
    "valid": 0,
    "dead": 0,
    "not_shopify": 0,
}
stats_lock = asyncio.Lock()


async def _increment_stat(key: str, amount: int = 1) -> None:
    async with stats_lock:
        bot_stats[key] += amount


async def _stats_snapshot() -> dict:
    async with stats_lock:
        return dict(bot_stats)


def _is_admin(user_id: int) -> bool:
    return ADMIN_ID is not None and user_id == ADMIN_ID


async def _ensure_access(update: Update) -> bool:
    user = update.effective_user
    if not user:
        return False

    if _is_admin(user.id) or is_authorized(user.id):
        return True

    if update.message:
        await update.message.reply_text("No Access")
    return False


async def _ensure_admin(update: Update) -> bool:
    user = update.effective_user
    if user and _is_admin(user.id):
        return True

    if update.message:
        await update.message.reply_text("No Access")
    return False


def _parse_proxy_text(text: str) -> List[str]:
    return [
        item.strip()
        for line in text.splitlines()
        for item in line.replace(",", " ").split()
        if item.strip()
    ]


async def start_command(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_access(update):
        return

    await update.message.reply_text(
        "Shopify Checker Bot is ready.\n"
        "Commands: /check <url>, /masscheck, /stats\n"
        "Admin: /adduser <id>, /removeuser <id>, /addproxy"
    )


async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_access(update):
        return

    if not context.args:
        await update.message.reply_text("Usage: /check <url>")
        return

    url = context.args[0]
    status_message = await update.message.reply_text("Checking store...")

    try:
        result = await check_shopify_store(url, proxy_manager, timeout=20)
    except Exception as exc:  # noqa: BLE001
        await status_message.edit_text(f"Check failed: {exc}")
        return

    await _increment_stat("single_checks")
    await _increment_stat("stores_checked")

    if result["status"] == "Valid Store":
        await _increment_stat("valid")
    elif result["status"] == "Not Shopify":
        await _increment_stat("not_shopify")
    else:
        await _increment_stat("dead")

    await status_message.edit_text(
        "\n".join(
            [
                f"URL: {result['normalized_url']}",
                f"Status: {result['status']}",
                f"Live: {'Yes' if result['live'] else 'No'}",
                f"Shopify Confirmed: {'Yes' if result['shopify_confirmed'] else 'No'}",
                f"Has Products: {'Yes' if result['has_products'] else 'No'}",
                f"Product Count: {result['product_count']}",
                f"Store Name: {result['store_name']}",
                f"Currency: {result['currency']}",
                f"Reason: {result['reason']}",
            ]
        )
    )


async def masscheck_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_access(update):
        return

    context.user_data["awaiting_masscheck_file"] = True
    await update.message.reply_text("Send a .txt file with one store URL per line.")


async def add_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_admin(update):
        return

    if not context.args:
        await update.message.reply_text("Usage: /adduser <user_id>")
        return

    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid user ID.")
        return

    added = add_user(user_id)
    await update.message.reply_text("User added." if added else "User already exists.")


async def remove_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_admin(update):
        return

    if not context.args:
        await update.message.reply_text("Usage: /removeuser <user_id>")
        return

    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid user ID.")
        return

    removed = remove_user(user_id)
    await update.message.reply_text("User removed." if removed else "User not found.")


async def add_proxy_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_admin(update):
        return

    if context.args:
        added = proxy_manager.add_proxies(_parse_proxy_text(" ".join(context.args)))
        await update.message.reply_text(f"Added {added} proxies. Total: {proxy_manager.count}")
        return

    context.user_data["awaiting_proxy_list"] = True
    await update.message.reply_text("Send proxy list (new line/comma/space separated).")


async def stats_command(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_access(update):
        return

    stats = await _stats_snapshot()
    await update.message.reply_text(
        "\n".join(
            [
                f"Authorized users: {count_users()}",
                f"Loaded proxies: {proxy_manager.count}",
                f"Single checks: {stats['single_checks']}",
                f"Mass checks: {stats['mass_checks']}",
                f"Stores checked: {stats['stores_checked']}",
                f"Valid stores: {stats['valid']}",
                f"Dead stores: {stats['dead']}",
                f"Not Shopify: {stats['not_shopify']}",
            ]
        )
    )


async def text_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    if context.user_data.get("awaiting_proxy_list"):
        if not await _ensure_admin(update):
            context.user_data["awaiting_proxy_list"] = False
            return

        added = proxy_manager.add_proxies(_parse_proxy_text(update.message.text))
        context.user_data["awaiting_proxy_list"] = False
        await update.message.reply_text(f"Added {added} proxies. Total: {proxy_manager.count}")


async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.document:
        return

    if not context.user_data.get("awaiting_masscheck_file"):
        return

    if not await _ensure_access(update):
        context.user_data["awaiting_masscheck_file"] = False
        return

    context.user_data["awaiting_masscheck_file"] = False

    try:
        telegram_file = await update.message.document.get_file()
        file_bytes = await telegram_file.download_as_bytearray()
        decoded = file_bytes.decode("utf-8", errors="ignore")
        urls = [line.strip() for line in decoded.splitlines() if line.strip()]
    except Exception as exc:  # noqa: BLE001
        await update.message.reply_text(f"Failed to read file: {exc}")
        return

    if not urls:
        await update.message.reply_text("No URLs found in file.")
        return

    progress_message = await update.message.reply_text(f"Checking 0/{len(urls)}...")

    valid = 0
    dead = 0
    not_shopify = 0
    results_lines = []

    last_progress_update = 0.0
    for index, url in enumerate(urls, start=1):
        try:
            result = await check_shopify_store(url, proxy_manager, timeout=20)
        except Exception as exc:  # noqa: BLE001
            result = {
                "normalized_url": url,
                "status": "Dead",
                "live": False,
                "shopify_confirmed": False,
                "has_products": False,
                "product_count": 0,
                "store_name": "Unknown",
                "currency": "Unknown",
                "reason": f"Check failed: {exc}",
            }

        if result["status"] == "Valid Store":
            valid += 1
            await _increment_stat("valid")
        elif result["status"] == "Not Shopify":
            not_shopify += 1
            await _increment_stat("not_shopify")
        else:
            dead += 1
            await _increment_stat("dead")

        await _increment_stat("stores_checked")

        results_lines.append(
            " | ".join(
                [
                    result["normalized_url"],
                    result["status"],
                    f"Live={result['live']}",
                    f"Shopify={result['shopify_confirmed']}",
                    f"Products={result['product_count']}",
                    f"Store={result['store_name']}",
                    f"Currency={result['currency']}",
                    f"Reason={result['reason']}",
                ]
            )
        )

        now = monotonic()
        if index == len(urls) or now - last_progress_update >= 1.2:
            await progress_message.edit_text(f"Checking {index}/{len(urls)}...")
            last_progress_update = now

    await _increment_stat("mass_checks")

    summary = (
        f"Mass Check Complete\n"
        f"Valid Stores: {valid}\n"
        f"Dead Stores: {dead}\n"
        f"Not Shopify: {not_shopify}"
    )

    output = io.BytesIO("\n".join(results_lines).encode("utf-8"))
    output.name = "masscheck_results.txt"

    await update.message.reply_document(document=InputFile(output), caption=summary)
    await progress_message.edit_text(summary)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled exception while processing update", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text("An internal error occurred. Try again.")


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set. Configure BOT_TOKEN in env or config.py")

    init_db(ADMIN_ID)

    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("check", check_command))
    application.add_handler(CommandHandler("masscheck", masscheck_command))
    application.add_handler(CommandHandler("adduser", add_user_command))
    application.add_handler(CommandHandler("removeuser", remove_user_command))
    application.add_handler(CommandHandler("addproxy", add_proxy_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(
        MessageHandler(filters.Document.TEXT & ~filters.COMMAND, document_handler)
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_message_handler)
    )
    application.add_error_handler(error_handler)

    application.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
