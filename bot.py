import os
import logging
import uuid
from datetime import datetime
from typing import List
from telegram import (
    Update,
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
from fastapi.responses import JSONResponse
import uvicorn

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
PORT = int(os.getenv("PORT", 8000))

# Database setup
client = AsyncIOMotorClient(DATABASE_URL)
db = client.file_bot_db
users_col = db.users
files_col = db.files
batches_col = db.batches

# FastAPI setup
web_app = FastAPI()

# Utility functions
async def is_admin(user_id: int) -> bool:
    return user_id in ADMINS


async def check_membership(user_id: int, bot) -> bool:
    try:
        for channel_id in FORCE_SUBS:
            member = await bot.get_chat_member(channel_id, user_id)
            if member.status in ["left", "kicked"]:
                return False
        return True
    except Exception as e:
        logger.error(f"Membership check error: {e}")
        return False


def generate_link(file_id: str, is_batch: bool = False) -> str:
    return f"{RAILWAY_STATIC_URL}/get/{'batch' if is_batch else 'file'}/{file_id}"


# Health check endpoint
@web_app.get("/health")
async def health_check():
    return JSONResponse(
        content={"status": "ok", "version": "1.0"},
        status_code=status.HTTP_200_OK,
    )


# File retrieval endpoints
@web_app.get("/get/file/{file_id}")
async def serve_file(file_id: str):
    file_data = await files_col.find_one({"_id": file_id})
    if not file_data:
        return JSONResponse(
            content={"error": "File not found"},
            status_code=status.HTTP_404_NOT_FOUND,
        )

    # Check membership
    user_id = int(file_id.split("_")[0])
    if not await check_membership(user_id, application.bot):
        keyboard = [
            [InlineKeyboardButton(f"Join Channel {i+1}", url=f"t.me/{cid}") for i, cid in enumerate(FORCE_SUBS)],
            [InlineKeyboardButton("Try Again", callback_data=f"verify_{file_id}")],
        ]
        return JSONResponse(
            content={
                "error": "You must join the required channels first!",
                "keyboard": keyboard,
            },
            status_code=status.HTTP_403_FORBIDDEN,
        )

    # Forward file from dump channel
    await application.bot.copy_message(user_id, DUMP_CHANNEL, file_data["message_id"])
    return JSONResponse(content={"status": "ok"})


@web_app.get("/get/batch/{batch_id}")
async def serve_batch(batch_id: str):
    batch_data = await batches_col.find_one({"_id": batch_id})
    if not batch_data:
        return JSONResponse(
            content={"error": "Batch not found"},
            status_code=status.HTTP_404_NOT_FOUND,
        )

    # Check membership
    user_id = int(batch_id.split("_")[0])
    if not await check_membership(user_id, application.bot):
        keyboard = [
            [InlineKeyboardButton(f"Join Channel {i+1}", url=f"t.me/{cid}") for i, cid in enumerate(FORCE_SUBS)],
            [InlineKeyboardButton("Try Again", callback_data=f"verify_{batch_id}")],
        ]
        return JSONResponse(
            content={
                "error": "You must join the required channels first!",
                "keyboard": keyboard,
            },
            status_code=status.HTTP_403_FORBIDDEN,
        )

    # Forward files from dump channel
    for file_entry in batch_data["files"]:
        await application.bot.copy_message(user_id, DUMP_CHANNEL, file_entry["message_id"])
    return JSONResponse(content={"status": "ok"})


# Telegram bot handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await users_col.update_one(
        {"_id": user.id},
        {"$set": {"username": user.username}},
        upsert=True,
    )
    if await is_admin(user.id):
        await update.message.reply_text("✅ Admin panel ready\nSend files or use /batch")
    else:
        await update.message.reply_text("❌ You need authorization to use this bot")


async def store_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not await is_admin(user.id):
        return

    try:
        msg = await update.message.forward(DUMP_CHANNEL)
        file = update.message.document or update.message.photo[-1] if update.message.photo else None

        file_id = f"{user.id}_{uuid.uuid4()}"
        await files_col.insert_one(
            {
                "_id": file_id,
                "message_id": msg.message_id,
                "file_id": file.file_id,
                "timestamp": datetime.now(),
                "uploader": user.id,
            }
        )

        await update.message.reply_text(f"✅ File stored!\n🔗 Permanent Link: {generate_link(file_id)}")
    except Exception as e:
        logger.error(f"File storage error: {e}")
        await update.message.reply_text("❌ Failed to store file")


async def handle_batch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not await is_admin(user.id):
        return

    if not context.user_data.get("batch_mode"):
        context.user_data.update(
            {
                "batch_mode": True,
                "batch_files": [],
            }
        )
        await update.message.reply_text("📦 Batch mode activated!\nSend files now. Use /endbatch when done")
    else:
        context.user_data["batch_files"].append(update.message)
        await update.message.reply_text("📎 File added to batch")


async def end_batch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not (files := context.user_data.get("batch_files")):
        await update.message.reply_text("⚠️ No files in batch")
        return

    try:
        batch_id = f"{user.id}_{uuid.uuid4()}"
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

        await update.message.reply_text(f"✅ Batch stored!\n🔗 Permanent Link: {generate_link(batch_id, True)}")
    except Exception as e:
        logger.error(f"Batch storage error: {e}")
        await update.message.reply_text("❌ Failed to store batch")
    finally:
        context.user_data.clear()


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return

    try:
        stats_data = {
            "users": await users_col.count_documents({}),
            "files": await files_col.count_documents({}),
            "batches": await batches_col.count_documents({}),
        }

        stats_msg = (
            "📊 Bot Statistics:\n"
            f"• Users: {stats_data['users']}\n"
            f"• Files: {stats_data['files']}\n"
            f"• Batches: {stats_data['batches']}"
        )
        await update.message.reply_text(stats_msg)
    except Exception as e:
        logger.error(f"Stats error: {e}")
        await update.message.reply_text("❌ Failed to get statistics")


async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text("❌ Provide a message to broadcast.")
        return

    message = " ".join(context.args)
    users = await users_col.find({}).to_list(None)

    for user in users:
        try:
            await context.bot.send_message(user["_id"], message)
        except Exception as e:
            logger.error(f"Broadcast error for user {user['_id']}: {e}")

    await update.message.reply_text("✅ Broadcast completed.")


# Setup handlers
def setup_handlers(application):
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("batch", handle_batch))
    application.add_handler(CommandHandler("endbatch", end_batch))
    application.add_handler(CommandHandler("broadcast", broadcast))
    application.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, store_file))


# Main entry point
if __name__ == "__main__":
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    setup_handlers(application)

    # Start FastAPI server
    @web_app.on_event("startup")
    async def startup():
        await application.initialize()
        await application.start()
        await application.updater.start_polling()
        logger.info("Bot started with long polling.")

    @web_app.on_event("shutdown")
    async def shutdown():
        await application.stop()
        await application.shutdown()
        logger.info("Bot shutdown complete.")

    uvicorn.run(
        web_app,
        host="0.0.0.0",
        port=PORT,
        log_level="info",
        timeout_keep_alive=3600,
    )
