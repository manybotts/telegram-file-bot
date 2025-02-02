import os
import uuid
import logging
import asyncio
from threading import Thread
from pymongo import MongoClient, errors
from telegram import Update, Bot, Message, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackContext,
    filters,
    CallbackQueryHandler,
)

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Validate environment variables
REQUIRED_ENV = [
    "BOT_TOKEN",
    "MONGO_URI",
    "ADMINS",
    "DUMP_CHANNEL_ID",
    "FORCE_SUB_CHANNELS",
    "RAILWAY_STATIC_URL"
]

try:
    BOT_TOKEN = os.environ["BOT_TOKEN"]
    MONGO_URI = os.environ["MONGO_URI"]
    ADMINS = [int(admin) for admin in os.environ["ADMINS"].split(",")]
    DUMP_CHANNEL_ID = int(os.environ["DUMP_CHANNEL_ID"])
    FORCE_SUB_CHANNELS = [int(channel) for channel in os.environ["FORCE_SUB_CHANNELS"].split(",")]
    RAILWAY_DOMAIN = os.environ["RAILWAY_STATIC_URL"]
except KeyError as e:
    logger.error(f"Missing required environment variable: {e}")
    exit(1)
except ValueError as e:
    logger.error(f"Invalid environment variable format: {e}")
    exit(1)

# Database setup with error handling
try:
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    client.server_info()  # Test connection
    db = client.telegram_file_bot
    files_collection = db.files
    batches_collection = db.batches
    
    # Create indexes
    files_collection.create_index("file_id", unique=True)
    batches_collection.create_index("batch_id", unique=True)
    batches_collection.create_index("media_group", unique=True)
except errors.ConnectionFailure:
    logger.error("Failed to connect to MongoDB")
    exit(1)
except errors.PyMongoError as e:
    logger.error(f"MongoDB error: {e}")
    exit(1)

async def setup_webhook(bot: Bot):
    """Automatically configure webhook on startup"""
    webhook_url = f"https://{RAILWAY_DOMAIN}/webhook/{BOT_TOKEN}"
    
    try:
        result = await bot.set_webhook(webhook_url)
        if result:
            logger.info(f"Webhook set successfully: {webhook_url}")
        else:
            logger.error("Webhook setup failed")
            exit(1)
    except Exception as e:
        logger.error(f"Webhook configuration error: {e}")
        exit(1)

async def start(update: Update, context: CallbackContext):
    """Handle start command with deep linking"""
    try:
        if context.args:
            file_id = context.args[0]
            await handle_file_request(update, context, file_id)
        else:
            await update.message.reply_text(
                "🔒 Secure File Sharing Bot\n\n"
                "Admins can upload files to generate shareable links. "
                "Users need to join required channels to access content."
            )
    except Exception as e:
        logger.error(f"Start command error: {e}")

async def handle_admin_upload(update: Update, context: CallbackContext):
    """Process admin file uploads"""
    try:
        user = update.effective_user
        if user.id not in ADMINS:
            await update.message.reply_text("⛔ Admin access required")
            return

        message = update.effective_message
        if message.media_group_id:
            await process_batch_files(message, user)
        else:
            await process_single_file(message, user)
    except Exception as e:
        logger.error(f"Upload handling error: {e}")
        await update.message.reply_text("⚠️ Error processing files")

async def process_single_file(message: Message, user):
    """Handle single file upload"""
    try:
        file_id = str(uuid.uuid4())
        forwarded = await message.forward(DUMP_CHANNEL_ID)
        
        files_collection.insert_one({
            "file_id": file_id,
            "message_id": forwarded.message_id,
            "type": "single",
            "owner": user.id
        })
        
        share_link = f"https://t.me/{message.bot.username}?start={file_id}"
        await message.reply_text(f"✅ File stored!\n🔗 Permanent link: {share_link}")
    except Exception as e:
        logger.error(f"Single file error: {e}")
        await message.reply_text("⚠️ Failed to process file")

async def process_batch_files(message: Message, user):
    """Handle batch file upload"""
    try:
        media_group = message.media_group_id
        if batches_collection.find_one({"media_group": media_group}):
            return

        batch_id = str(uuid.uuid4())
        batches_collection.insert_one({
            "batch_id": batch_id,
            "media_group": media_group,
            "owner": user.id,
            "message_ids": []
        })

        messages = await message.bot.get_media_group(
            chat_id=message.chat_id,
            message_id=message.message_id
        )

        for msg in messages:
            forwarded = await msg.forward(DUMP_CHANNEL_ID)
            batches_collection.update_one(
                {"batch_id": batch_id},
                {"$push": {"message_ids": forwarded.message_id}}
            )

        share_link = f"https://t.me/{message.bot.username}?start={batch_id}"
        await message.reply_text(f"✅ Batch stored!\n🔗 Permanent link: {share_link}")
    except Exception as e:
        logger.error(f"Batch processing error: {e}")
        await message.reply_text("⚠️ Failed to process batch")

async def handle_file_request(update: Update, context: CallbackContext, file_id: str):
    """Handle file access requests"""
    try:
        user = update.effective_user
        if not await verify_subscription(user.id, context.bot):
            await show_subscription_required(update, context, file_id)
            return

        # Retrieve and send files
        file_data = files_collection.find_one({"file_id": file_id})
        batch_data = batches_collection.find_one({"batch_id": file_id})

        if file_data:
            await context.bot.forward_message(
                chat_id=user.id,
                from_chat_id=DUMP_CHANNEL_ID,
                message_id=file_data["message_id"]
            )
        elif batch_data:
            for msg_id in batch_data["message_ids"]:
                await context.bot.forward_message(
                    chat_id=user.id,
                    from_chat_id=DUMP_CHANNEL_ID,
                    message_id=msg_id
                )
        else:
            await update.message.reply_text("⚠️ Invalid or expired link")
    except Exception as e:
        logger.error(f"File request error: {e}")

async def verify_subscription(user_id: int, bot: Bot) -> bool:
    """Check channel subscriptions"""
    try:
        for channel_id in FORCE_SUB_CHANNELS:
            member = await bot.get_chat_member(channel_id, user_id)
            if member.status in ["left", "kicked"]:
                return False
        return True
    except Exception as e:
        logger.error(f"Subscription check error: {e}")
        return False

async def show_subscription_required(update: Update, context: CallbackContext, file_id: str):
    """Show subscription prompt with buttons"""
    try:
        buttons = []
        for channel_id in FORCE_SUB_CHANNELS:
            channel = await context.bot.get_chat(channel_id)
            buttons.append([InlineKeyboardButton(
                text=f"Join {channel.title}",
                url=f"https://t.me/{channel.username}"
            )])
        
        buttons.append([InlineKeyboardButton(
            text="✅ I've Joined - Try Again",
            callback_data=f"verify_{file_id}"
        )])

        await update.message.reply_text(
            "📢 You must join these channels to access content:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    except Exception as e:
        logger.error(f"Subscription prompt error: {e}")

async def handle_callback(update: Update, context: CallbackContext):
    """Handle callback queries"""
    try:
        query = update.callback_query
        if query.data.startswith("verify_"):
            file_id = query.data.split("_")[1]
            if await verify_subscription(query.from_user.id, context.bot):
                await query.message.delete()
                await handle_file_request(update, context, file_id)
            else:
                await query.answer("Please join all channels first!", show_alert=True)
    except Exception as e:
        logger.error(f"Callback error: {e}")

async def main():
    """Main application setup"""
    try:
        application = Application.builder().token(BOT_TOKEN).build()
        
        # Register handlers
        application.add_handlers([
            CommandHandler("start", start),
            MessageHandler(filters.Document | filters.PHOTO | filters.VIDEO, handle_admin_upload),
            CallbackQueryHandler(handle_callback)
        ])

        # Auto-configure webhook in production
        await setup_webhook(application.bot)
        
        logger.info("Bot is running")
        await application.run_polling()

    except Exception as e:
        logger.error(f"Fatal error: {e}")
        exit(1)

if __name__ == "__main__":
    asyncio.run(main())
