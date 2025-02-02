import os
import uuid
import logging
from typing import List, Dict
from threading import Thread
from pymongo import MongoClient
from telegram import (
    Update,
    Bot,
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputMediaDocument,
)
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

# Environment variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
ADMINS = [int(admin) for admin in os.getenv("ADMINS").split(",")]
DUMP_CHANNEL_ID = int(os.getenv("DUMP_CHANNEL_ID"))
FORCE_SUB_CHANNELS = [int(channel) for channel in os.getenv("FORCE_SUB_CHANNELS").split(",")]

# Database setup
client = MongoClient(MONGO_URI)
db = client.telegram_file_bot
files_collection = db.files
batches_collection = db.batches

async def start(update: Update, context: CallbackContext):
    """Handle start command with deep linking"""
    user = update.effective_user
    args = context.args
    
    if args:
        file_id = args[0]
        await handle_file_request(update, context, file_id)
    else:
        await update.message.reply_text(
            "Welcome! This bot provides secure file sharing. "
            "Only admins can upload files."
        )

async def handle_admin_upload(update: Update, context: CallbackContext):
    """Process admin file uploads"""
    if update.effective_user.id not in ADMINS:
        await update.message.reply_text("Unauthorized access.")
        return

    message = update.effective_message
    file_type = "batch" if message.media_group_id else "single"
    
    if file_type == "single":
        await process_single_file(update, context)
    else:
        await process_batch_files(update, context)

async def process_single_file(update: Update, context: CallbackContext):
    """Handle single file upload"""
    message = update.effective_message
    file_id = str(uuid.uuid4())
    
    # Forward file to dump channel
    forwarded = await message.forward(DUMP_CHANNEL_ID)
    
    # Store metadata
    files_collection.insert_one({
        "file_id": file_id,
        "message_id": forwarded.message_id,
        "type": "single",
        "owner": update.effective_user.id
    })
    
    # Generate shareable link
    share_link = f"https://t.me/{context.bot.username}?start={file_id}"
    await message.reply_text(f"File stored!\nShare link: {share_link}")

async def process_batch_files(update: Update, context: CallbackContext):
    """Handle batch file upload"""
    batch_id = str(uuid.uuid4())
    message = update.effective_message
    media_group = message.media_group_id
    
    # Check if batch already being processed
    if batches_collection.find_one({"media_group": media_group}):
        return
    
    # Store batch info
    batches_collection.insert_one({
        "batch_id": batch_id,
        "media_group": media_group,
        "owner": update.effective_user.id,
        "message_ids": []
    })
    
    # Process media group
    messages = await get_media_group_messages(context.bot, message)
    file_ids = []
    
    for msg in messages:
        forwarded = await msg.forward(DUMP_CHANNEL_ID)
        batches_collection.update_one(
            {"batch_id": batch_id},
            {"$push": {"message_ids": forwarded.message_id}}
        )
    
    # Generate batch link
    share_link = f"https://t.me/{context.bot.username}?start={batch_id}"
    await message.reply_text(f"Batch stored!\nShare link: {share_link}")

async def handle_file_request(update: Update, context: CallbackContext, file_id: str):
    """Handle file requests from share links"""
    user = update.effective_user
    bot = context.bot
    
    # Check force subscription
    if not await check_subscription(user.id, bot):
        await send_subscription_required(update, context, file_id)
        return
    
    # Retrieve file(s)
    if files_collection.find_one({"file_id": file_id}):
        file_data = files_collection.find_one({"file_id": file_id})
        await bot.forward_message(
            chat_id=user.id,
            from_chat_id=DUMP_CHANNEL_ID,
            message_id=file_data["message_id"]
        )
    elif batches_collection.find_one({"batch_id": file_id}):
        batch_data = batches_collection.find_one({"batch_id": file_id})
        for msg_id in batch_data["message_ids"]:
            await bot.forward_message(
                chat_id=user.id,
                from_chat_id=DUMP_CHANNEL_ID,
                message_id=msg_id
            )

async def check_subscription(user_id: int, bot: Bot) -> bool:
    """Check if user is subscribed to required channels"""
    for channel_id in FORCE_SUB_CHANNELS:
        try:
            member = await bot.get_chat_member(channel_id, user_id)
            if member.status in ["left", "kicked"]:
                return False
        except Exception as e:
            logger.error(f"Subscription check failed: {e}")
            return False
    return True

async def send_subscription_required(update: Update, context: CallbackContext, file_id: str):
    """Send subscription reminder with buttons"""
    buttons = []
    for idx, channel_id in enumerate(FORCE_SUB_CHANNELS, 1):
        channel = await context.bot.get_chat(channel_id)
        buttons.append([InlineKeyboardButton(
            text=f"Join Channel {idx}",
            url=f"https://t.me/{channel.username}"
        )])
    
    buttons.append([InlineKeyboardButton(
        text="Try Again",
        callback_data=f"check_sub_{file_id}"
    )])
    
    await update.message.reply_text(
        "Please join our channels to access this content:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def handle_callback(update: Update, context: CallbackContext):
    """Handle callback queries"""
    query = update.callback_query
    data = query.data
    
    if data.startswith("check_sub_"):
        file_id = data.split("_")[-1]
        if await check_subscription(query.from_user.id, context.bot):
            await query.message.delete()
            await handle_file_request(update, context, file_id)
        else:
            await query.answer("Please join all channels first!", show_alert=True)

async def get_media_group_messages(bot: Bot, message: Message) -> List[Message]:
    """Retrieve all messages in a media group"""
    return await bot.get_media_group(
        chat_id=message.chat_id,
        message_id=message.message_id
    )

def setup_handlers(application: Application):
    """Configure bot handlers"""
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(
        filters.Document | filters.PHOTO | filters.VIDEO,
        handle_admin_upload
    ))
    application.add_handler(CallbackQueryHandler(handle_callback))

async def run_bot():
    """Main bot runner"""
    application = Application.builder().token(BOT_TOKEN).build()
    setup_handlers(application)
    
    if os.getenv("RAILWAY_ENVIRONMENT") == "production":
        webhook_url = f"https://{os.getenv('RAILWAY_STATIC_URL')}/webhook"
        await application.start_webhook(
            listen="0.0.0.0",
            port=int(os.getenv("PORT", 8443)),
            url_path=BOT_TOKEN,
            webhook_url=webhook_url
        )
    else:
        await application.start_polling()

    await application.stop()

if __name__ == "__main__":
    Thread(target=lambda: asyncio.run(run_bot())).start()
