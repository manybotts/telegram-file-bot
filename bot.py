import os
import logging
import uuid
from datetime import datetime
from typing import Dict, List
from telegram import (
    Update,
    Bot,
    InputMediaDocument,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
    CallbackQueryHandler,
)
from motor.motor_asyncio import AsyncIOMotorClient
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
import uvicorn
import asyncio

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Environment variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMINS = list(map(int, os.getenv("ADMINS", "").split(",")))
DUMP_CHANNEL = int(os.getenv("DUMP_CHANNEL"))
FORCE_SUBS = list(map(int, os.getenv("FORCE_SUBS", "").split(",")))
DATABASE_URL = os.getenv("DATABASE_URL")
RAILWAY_STATIC_URL = os.getenv("RAILWAY_STATIC_URL")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
BOT_USERNAME = os.getenv("BOT_USERNAME")
PORT = int(os.getenv("PORT", 8000))

# Database setup
client = AsyncIOMotorClient(DATABASE_URL)
db = client.file_bot_db
users_col = db.users
files_col = db.files
batches_col = db.batches

# FastAPI setup
web_app = FastAPI()
application = ApplicationBuilder().token(BOT_TOKEN).build()

# Utility functions
async def is_admin(user_id: int) -> bool:
    logger.info(f"Checking if user {user_id} is an admin.")
    return user_id in ADMINS


async def check_membership(user_id: int, bot: Bot) -> bool:
    logger.info(f"Checking membership for user {user_id}.")
    try:
        for channel_id in FORCE_SUBS:
            member = await bot.get_chat_member(channel_id, user_id)
            logger.debug(f"User {user_id} status in channel {channel_id}: {member.status}")
            if member.status in ["left", "kicked"]:
                logger.warning(f"User {user_id} is not a member of channel {channel_id}.")
                return False
        logger.info(f"User {user_id} is a member of all required channels.")
        return True
    except Exception as e:
        logger.error(f"Membership check error for user {user_id}: {e}")
        return False


def generate_link(file_id: str, is_batch: bool = False) -> str:
    logger.info(f"Generating link for file_id: {file_id}, is_batch: {is_batch}.")
    return f"{RAILWAY_STATIC_URL}/get/{'batch' if is_batch else 'file'}/{file_id}"


# Health check endpoint
@web_app.get("/health")
async def health_check():
    logger.info("Health check endpoint accessed.")
    return JSONResponse(
        content={"status": "ok", "version": "1.0"},
        status_code=status.HTTP_200_OK,
    )


# Webhook processing endpoint
@web_app.post(f"/telegram")
async def process_webhook(request: Request):
    logger.info("Webhook endpoint accessed.")
    try:
        # Validate secret token (if used)
        if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
            logger.warning("Invalid secret token provided.")
            return JSONResponse(
                content={"error": "Forbidden"},
                status_code=status.HTTP_403_FORBIDDEN,
            )

        # Process incoming update
        data = await request.json()
        logger.debug(f"Incoming webhook data: {data}")
        update = Update.de_json(data, application.bot)
        logger.info(f"Processing update with ID: {update.update_id}")
        await application.update_queue.put(update)
        return JSONResponse(content={"status": "ok"})
    except Exception as e:
        logger.error(f"Error processing webhook: {e}")
        return JSONResponse(
            content={"error": "Internal Server Error", "detail": str(e)},
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


# Start event for FastAPI
@web_app.on_event("startup")
async def startup():
    logger.info("Starting bot...")
    await application.initialize()
    await application.start()

    # Check and set webhook
    webhook_info = await application.bot.get_webhook_info()
    expected_url = f"{RAILWAY_STATIC_URL}/telegram"

    if webhook_info.url != expected_url or webhook_info.last_error_date:
        logger.info(f"Setting webhook to: {expected_url}")
        while True:
            try:
                await application.bot.set_webhook(
                    url=expected_url,
                    secret_token=WEBHOOK_SECRET,
                )
                break
            except Exception as e:
                logger.error(f"Failed to set webhook: {e}")
                if "Flood control exceeded" in str(e):
                    logger.info("Flood control triggered. Retrying in 1 second...")
                    await asyncio.sleep(1)
                else:
                    raise  # Re-raise other exceptions
    else:
        logger.info("Webhook already configured correctly")

    logger.info("Bot startup complete")


# Shutdown event for FastAPI
@web_app.on_event("shutdown")
async def shutdown():
    logger.info("Shutting down bot...")
    await application.stop()
    await application.shutdown()
    logger.info("Bot shutdown complete")


# File retrieval endpoints
@web_app.get("/get/file/{file_id}")
async def serve_file(file_id: str):
    logger.info(f"File retrieval request for file_id: {file_id}.")
    file_data = await files_col.find_one({"_id": file_id})
    if not file_data:
        logger.warning(f"File not found for file_id: {file_id}.")
        return JSONResponse(
            content={"error": "File not found"},
            status_code=status.HTTP_404_NOT_FOUND,
        )

    file_path = await application.bot.get_file(file_data["file_id"])
    logger.info(f"Redirecting to file path: {file_path.file_path}.")
    return RedirectResponse(file_path.file_path)


@web_app.get("/get/batch/{batch_id}")
async def serve_batch(batch_id: str):
    logger.info(f"Batch retrieval request for batch_id: {batch_id}.")
    batch_data = await batches_col.find_one({"_id": batch_id})
    if not batch_data:
        logger.warning(f"Batch not found for batch_id: {batch_id}.")
        return JSONResponse(
            content={"error": "Batch not found"},
            status_code=status.HTTP_404_NOT_FOUND,
        )

    file_urls = []
    for file_entry in batch_data["files"]:
        file_path = await application.bot.get_file(file_entry["file_id"])
        file_urls.append(file_path.file_path)
        logger.debug(f"Added file path {file_path.file_path} to batch response.")

    logger.info(f"Returning batch file URLs: {file_urls}.")
    return {"file_urls": file_urls}


# Telegram bot handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f"Start command received from user {user.id}.")
    await users_col.update_one(
        {"_id": user.id},
        {"$set": {"username": user.username}},
        upsert=True,
    )
    logger.debug(f"User {user.id} registered/updated in the database.")

    if context.args:
        arg = context.args[0]
        if arg.startswith("file_"):
            logger.info(f"Handling file request for file_id: {arg[5:]}.")
            await handle_file_request(update, context, arg[5:])
        elif arg.startswith("batch_"):
            logger.info(f"Handling batch request for batch_id: {arg[6:]}.")
            await handle_batch_request(update, context, arg[6:])
    else:
        if await is_admin(user.id):
            logger.info(f"Admin user {user.id} accessing the bot.")
            await update.message.reply_text("✅ Admin panel ready\nSend files or use /batch")
        else:
            logger.warning(f"Unauthorized access attempt by user {user.id}.")
            await update.message.reply_text("❌ You need authorization to use this bot")


async def handle_file_request(update: Update, context: ContextTypes.DEFAULT_TYPE, file_id: str):
    user = update.effective_user
    logger.info(f"File request received for file_id: {file_id} by user {user.id}.")
    file_data = await files_col.find_one({"_id": file_id})

    if not file_data:
        logger.warning(f"File not found for file_id: {file_id}.")
        await update.message.reply_text("⚠️ File not found")
        return

    if await check_membership(user.id, context.bot):
        logger.info(f"User {user.id} is authorized to retrieve file {file_id}.")
        await context.bot.copy_message(user.id, DUMP_CHANNEL, file_data["message_id"])
    else:
        logger.warning(f"User {user.id} is not subscribed to required channels.")
        keyboard = [
            [
                InlineKeyboardButton(f"Join Channel {i+1}", url=f"t.me/{cid}")
                for i, cid in enumerate(FORCE_SUBS)
            ],
            [InlineKeyboardButton("✅ Verify Subscription", callback_data=f"verify_{file_id}")],
        ]
        await update.message.reply_text(
            "📢 You must join our channels first!",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    logger.info(f"Callback query received: {query.data}.")
    await query.answer()

    if query.data.startswith("verify_"):
        file_id = query.data[7:]
        logger.info(f"Verifying subscription for file_id: {file_id}.")
        if await check_membership(query.from_user.id, query.bot):
            file_data = await files_col.find_one({"_id": file_id})
            if file_data:
                logger.info(f"User {query.from_user.id} is authorized to retrieve file {file_id}.")
                await query.bot.copy_message(
                    query.from_user.id, DUMP_CHANNEL, file_data["message_id"]
                )
                await query.message.delete()
            else:
                logger.warning(f"File not found for file_id: {file_id}.")
                await query.message.edit_text("⚠️ File not found")
        else:
            logger.warning(f"User {query.from_user.id} is still not subscribed.")
            await query.message.edit_text("❌ Still not joined all channels!")


async def store_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f"Store file command received from user {user.id}.")
    if not await is_admin(user.id):
        logger.warning(f"Unauthorized file upload attempt by user {user.id}.")
        return

    try:
        msg = await update.message.forward(DUMP_CHANNEL)
        file = (
            update.message.document
            or update.message.photo[-1]
            if update.message.photo
            else None
        )

        file_id = str(uuid.uuid4())
        await files_col.insert_one(
            {
                "_id": file_id,
                "message_id": msg.message_id,
                "file_id": file.file_id,
                "timestamp": datetime.now(),
                "uploader": user.id,
            }
        )
        logger.info(f"File stored successfully with file_id: {file_id}.")

        await update.message.reply_text(
            f"✅ File stored!\n🔗 Permanent Link: {generate_link(file_id)}"
        )
    except Exception as e:
        logger.error(f"Error storing file for user {user.id}: {e}")
        await update.message.reply_text("❌ Failed to store file")


async def handle_batch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f"Batch mode activated by user {user.id}.")
    if not await is_admin(user.id):
        logger.warning(f"Unauthorized batch mode activation attempt by user {user.id}.")
        return

    if not context.user_data.get("batch_mode"):
        context.user_data.update(
            {
                "batch_mode": True,
                "batch_files": [],
                "batch_messages": [],
            }
        )
        logger.debug(f"Batch mode initialized for user {user.id}.")
        await update.message.reply_text(
            "📦 Batch mode activated!\nSend files now. Use /endbatch when done"
        )
    else:
        context.user_data["batch_files"].append(update.message)
        logger.debug(f"File added to batch for user {user.id}.")
        await update.message.reply_text("📎 File added to batch")


async def end_batch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f"End batch command received from user {user.id}.")
    if not (files := context.user_data.get("batch_files")):
        logger.warning(f"No files in batch for user {user.id}.")
        await update.message.reply_text("⚠️ No files in batch")
        return

    try:
        batch_id = str(uuid.uuid4())
        messages = []

        for f in files:
            msg = await f.forward(DUMP_CHANNEL)
            messages.append(
                {
                    "message_id": msg.message_id,
                    "file_id": f.document.file_id,
                }
            )

        await batches_col.insert_one(
            {
                "_id": batch_id,
                "files": messages,
                "timestamp": datetime.now(),
            }
        )
        logger.info(f"Batch stored successfully with batch_id: {batch_id}.")

        await update.message.reply_text(
            f"✅ Batch stored!\n🔗 Permanent Link: {generate_link(batch_id, True)}"
        )
    except Exception as e:
        logger.error(f"Error storing batch for user {user.id}: {e}")
        await update.message.reply_text("❌ Failed to store batch")
    finally:
        context.user_data.clear()


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f"Stats command received from user {user.id}.")
    if not await is_admin(user.id):
        logger.warning(f"Unauthorized stats access attempt by user {user.id}.")
        return

    try:
        stats_data = {
            "users": await users_col.count_documents({}),
            "files": await files_col.count_documents({}),
            "batches": await batches_col.count_documents({}),
            "storage": (await db.command("dbstats"))["dataSize"] / 1024 / 1024,
        }

        stats_msg = (
            "📊 Bot Statistics:\n"
            f"• Users: {stats_data['users']}\n"
            f"• Files: {stats_data['files']}\n"
            f"• Batches: {stats_data['batches']}\n"
            f"• Storage Used: {stats_data['storage']:.2f} MB"
        )
        logger.info(f"Stats message prepared: {stats_msg}.")
        await update.message.reply_text(stats_msg)
    except Exception as e:
        logger.error(f"Error fetching stats for user {user.id}: {e}")
        await update.message.reply_text("❌ Failed to get statistics")


# Setup handlers
def setup_handlers():
    logger.info("Setting up bot handlers...")
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("batch", handle_batch))
    application.add_handler(CommandHandler("endbatch", end_batch))
    application.add_handler(
        MessageHandler(filters.Document.ALL | filters.PHOTO, store_file)
    )
    application.add_handler(CallbackQueryHandler(handle_callback))


# Main entry point
if __name__ == "__main__":
    setup_handlers()
    logger.info("Starting bot server...")
    uvicorn.run(
        web_app,
        host="0.0.0.0",
        port=PORT,
        log_level="info",
        timeout_keep_alive=3600,
    )
