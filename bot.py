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
from pymongo import MongoClient
from pymongo.collection import Collection
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Environment variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMINS = list(map(int, os.getenv("ADMINS").split(",")))
DUMP_CHANNEL = int(os.getenv("DUMP_CHANNEL"))
FORCE_SUBS = list(map(int, os.getenv("FORCE_SUBS").split(",")))
DATABASE_URL = os.getenv("DATABASE_URL")
RAILWAY_STATIC_URL = os.getenv("RAILWAY_STATIC_URL")
CUSTOM_DOMAIN = os.getenv("CUSTOM_DOMAIN", "")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "DEFAULT_SECRET")
BOT_USERNAME = os.getenv("BOT_USERNAME")

# Database setup
client = MongoClient(DATABASE_URL)
db = client.file_bot_db

users_col: Collection = db.users
files_col: Collection = db.files
batches_col: Collection = db.batches

# FastAPI setup
web_app = FastAPI()
BASE_DOMAIN = CUSTOM_DOMAIN or RAILWAY_STATIC_URL

# Utility functions
async def is_admin(user_id: int) -> bool:
    return user_id in ADMINS

async def check_membership(user_id: int, bot: Bot) -> bool:
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
    link_type = "batch" if is_batch else "file"
    return f"{BASE_DOMAIN}/get/{link_type}/{file_id}"

# Web endpoints
@web_app.get("/get/file/{file_id}")
async def serve_file(request: Request, file_id: str):
    return RedirectResponse(f"https://t.me/{BOT_USERNAME}?start=file_{file_id}")

@web_app.get("/get/batch/{batch_id}")
async def serve_batch(request: Request, batch_id: str):
    return RedirectResponse(f"https://t.me/{BOT_USERNAME}?start=batch_{batch_id}")

@web_app.get("/health")
async def health_check():
    return {"status": "healthy"}

# Telegram bot handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    users_col.update_one(
        {"_id": user_id},
        {"$set": {"username": update.effective_user.username}},
        upsert=True,
    )

    if context.args:
        arg = context.args[0]
        if arg.startswith("file_"):
            await handle_file_request(update, context, arg[5:])
        elif arg.startswith("batch_"):
            await handle_batch_request(update, context, arg[6:])
    else:
        if await is_admin(user_id):
            await update.message.reply_text("Admin panel ready. Send files or use /batch")
        else:
            await update.message.reply_text("You need special access to use this bot")

async def handle_file_request(update: Update, context: ContextTypes.DEFAULT_TYPE, file_id: str):
    user_id = update.effective_user.id
    if not (file_data := files_col.find_one({"_id": file_id})):
        await update.message.reply_text("File not found")
        return

    if await check_membership(user_id, context.bot):
        await context.bot.copy_message(user_id, DUMP_CHANNEL, file_data["message_id"])
    else:
        keyboard = [
            [InlineKeyboardButton(f"Join Channel {i+1}", url=f"t.me/{cid}") 
             for i, cid in enumerate(FORCE_SUBS)],
            [InlineKeyboardButton("Try Again", callback_data=f"verify_{file_id}")]
        ]
        await update.message.reply_text(
            "Join required channels first!",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data.startswith("verify_"):
        file_id = query.data[7:]
        if await check_membership(query.from_user.id, query.bot):
            file_data = files_col.find_one({"_id": file_id})
            await query.bot.copy_message(query.from_user.id, DUMP_CHANNEL, file_data["message_id"])
            await query.message.delete()
        else:
            await query.message.reply_text("Still not joined all channels!")

async def store_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await is_admin(user_id):
        return

    msg = await update.message.forward(DUMP_CHANNEL)
    file = update.message.document or update.message.photo[-1] if update.message.photo else None
    
    file_id = str(uuid.uuid4())
    files_col.insert_one({
        "_id": file_id,
        "message_id": msg.message_id,
        "file_id": file.file_id,
        "timestamp": datetime.now(),
        "uploader": user_id
    })
    
    await update.message.reply_text(f"File stored!\nPermanent Link: {generate_link(file_id)}")

async def handle_batch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await is_admin(user_id):
        return

    if not context.user_data.get("batch_mode"):
        context.user_data["batch_mode"] = True
        context.user_data["batch_files"] = []
        await update.message.reply_text("Batch mode activated! Send files now. /endbatch when done")
    else:
        context.user_data["batch_files"].append(update.message)
        await update.message.reply_text("File added to batch!")

async def end_batch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not (files := context.user_data.get("batch_files")):
        await update.message.reply_text("No files in batch!")
        return

    batch_id = str(uuid.uuid4())
    messages = [await f.forward(DUMP_CHANNEL) for f in files]
    
    batches_col.insert_one({
        "_id": batch_id,
        "files": [{
            "message_id": m.message_id,
            "file_id": f.document.file_id
        } for m, f in zip(messages, files)],
        "timestamp": datetime.now()
    })
    
    await update.message.reply_text(f"Batch stored!\nPermanent Link: {generate_link(batch_id, True)}")
    context.user_data.clear()

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return
    
    stats_msg = f"""📊 Bot Statistics:
Users: {users_col.count_documents({})}
Files: {files_col.count_documents({})}
Batches: {batches_col.count_documents({})}
Storage Used: {db.command("dbstats")['dataSize']/1024/1024:.2f} MB"""
    await update.message.reply_text(stats_msg)

def main() -> None:
    app = ApplicationBuilder().token(BOT_TOKEN).updater(None).build()
    
    # Command handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("batch", handle_batch))
    app.add_handler(CommandHandler("endbatch", end_batch))
    app.add_handler(CommandHandler("stats", stats))
    
    # File handlers
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, store_file))
    app.add_handler(CallbackQueryHandler(handle_callback))
    
    # Webhook setup
    @web_app.post("/telegram")
    async def process_webhook(request: Request):
        data = await request.json()
        update = Update.de_json(data, app.bot)
        await app.update_queue.put(update)
        return {"status": "ok"}

    webhook_url = f"{BASE_DOMAIN}/telegram"
    
    app.run_webhook(
        listen="0.0.0.0",
        port=int(os.getenv("PORT", 8000)),
        webhook_url=webhook_url,
        secret_token=WEBHOOK_SECRET,
        fastapi_app=web_app
    )

if __name__ == "__main__":
    main()
