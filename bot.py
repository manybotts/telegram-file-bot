import os
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    CallbackQueryHandler,
    CallbackContext,
)
import asyncio

# Initialize logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Load environment variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
FORCE_SUB_CHANNELS = [int(channel_id) for channel_id in os.getenv("FORCE_SUB_CHANNELS", "").split(",") if channel_id]

# Helper functions
async def verify_subscription(user_id: int, bot):
    """Verify if a user is subscribed to all required channels."""
    try:
        for channel_id in FORCE_SUB_CHANNELS:
            chat_member = await bot.get_chat_member(chat_id=channel_id, user_id=user_id)
            if chat_member.status in ["left", "kicked"]:
                return False
        return True
    except Exception as e:
        logger.error(f"Subscription verification error: {e}")
        return False


# Handlers
async def start(update: Update, context: CallbackContext):
    """Handle /start command."""
    try:
        file_id = None
        if context.args and len(context.args) > 0:
            file_id = context.args[0]

        if not await verify_subscription(update.effective_user.id, context.bot):
            buttons = []
            for channel_id in FORCE_SUB_CHANNELS:
                channel = await context.bot.get_chat(channel_id)
                buttons.append(
                    [
                        InlineKeyboardButton(
                            text=f"Join {channel.title}",
                            url=f"https://t.me/{channel.username}",
                        )
                    ]
                )

            buttons.append(
                [
                    InlineKeyboardButton(
                        text="✅ I've Joined - Try Again",
                        callback_data=f"verify_{file_id}" if file_id else "verify",
                    )
                ]
            )
            await update.message.reply_text(
                "📢 You must join these channels to access content:",
                reply_markup=InlineKeyboardMarkup(buttons),
            )
            return

        if file_id:
            await handle_file_request(update, context, file_id)
        else:
            await update.message.reply_text("Welcome! Use this bot to access content.")
    except Exception as e:
        logger.error(f"Start command error: {e}")


async def handle_admin_upload(update: Update, context: CallbackContext):
    """Handle file uploads from admins."""
    try:
        if update.effective_user.id not in ADMINS:
            await update.message.reply_text("You are not authorized to upload files.")
            return

        # Process uploaded file (store it, generate link, etc.)
        file_obj = update.message.document or update.message.photo[-1] or update.message.video
        file_info = await context.bot.get_file(file_obj.file_id)

        # Store file metadata in a database or other storage
        file_id = file_obj.file_id
        file_unique_id = file_obj.file_unique_id

        # Example: Save file details to a database (replace with actual DB logic)
        logger.info(f"File uploaded: {file_unique_id}")

        # Generate and send file link
        file_link = f"https://t.me/{context.bot.username}?start={file_unique_id}"
        await update.message.reply_text(f"File uploaded successfully!\nLink: {file_link}")
    except Exception as e:
        logger.error(f"File upload error: {e}")


async def handle_callback(update: Update, context: CallbackContext):
    """Handle callback queries."""
    try:
        query = update.callback_query
        await query.answer()

        if query.data.startswith("verify_"):
            file_id = query.data.split("_")[1] if "_" in query.data else None
            if await verify_subscription(query.from_user.id, context.bot):
                await query.message.delete()
                if file_id:
                    await handle_file_request(update, context, file_id)
                else:
                    await query.message.reply_text("You are now verified!")
            else:
                await query.message.edit_text(
                    "Please join all channels to access content.", show_alert=True
                )
    except Exception as e:
        logger.error(f"Callback error: {e}")


async def handle_file_request(update: Update, context: CallbackContext, file_id: str):
    """Handle file requests by sending the file to the user."""
    try:
        # Example: Fetch file details from database or storage (replace with actual logic)
        file_info = {"file_id": file_id}  # Replace with actual file retrieval logic

        if not file_info:
            await update.message.reply_text("File not found.")
            return

        await context.bot.send_document(chat_id=update.effective_chat.id, document=file_info["file_id"])
    except Exception as e:
        logger.error(f"File request error: {e}")


# Main application setup
async def main():
    try:
        application = Application.builder().token(BOT_TOKEN).build()

        # Register handlers
        application.add_handlers(
            [
                CommandHandler("start", start),
                # Corrected filters for handling documents, photos, and videos
                MessageHandler(
                    filters.Document.ALL | filters.PHOTO.ALL | filters.VIDEO.ALL,
                    handle_admin_upload,
                ),
                CallbackQueryHandler(handle_callback),
            ]
        )

        # Auto-configure webhook in production
        if RAILWAY_STATIC_URL := os.getenv("RAILWAY_STATIC_URL"):
            webhook_url = f"{RAILWAY_STATIC_URL}/"
            await application.bot.set_webhook(url=webhook_url)
            logger.info(f"Webhook set successfully: {webhook_url}")
        else:
            logger.warning("RAILWAY_STATIC_URL not set. Running in polling mode.")
            await application.run_polling()

        logger.info("Bot is running")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        exit(1)


if __name__ == "__main__":
    asyncio.run(main())
