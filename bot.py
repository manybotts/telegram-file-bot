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
from fastapi.responses import JSONResponse
import uvicorn

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", 
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Environment variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMINS = list(map(int, os.getenv("ADMINS").split(",")))
DUMP_CHANNEL = int(os.getenv("DUMP_CHANNEL"))
FORCE_SUBS = list(map(int, os.getenv("FORCE_SUBS").split(",")))
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
    return f"{RAILWAY_STATIC_URL}/get/{'batch' if is_batch else 'file'}/{file_id}"

@web_app.get("/health")
async def health_check():
    return JSONResponse(
        content={"status": "ok", "version": "1.0"},
        status_code=status.HTTP_200_OK
    )

@web_app.post(f"/telegram")
async def process_webhook(request: Request):
    try:
        data = await request.json()
        update = Update.de_json(data, application.bot)
        await application.update_queue.put(update)
        return JSONResponse(content={"status": "ok"})
    except Exception as e:
        logger.error(f"Webhook processing error: {e}")
        return JSONResponse(
            content={"status": "error", "detail": str(e)},
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
        )

@web_app.on_event("startup")
async def startup():
    await application.initialize()
    await application.start()
    await application.updater.start_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=BOT_TOKEN,
        webhook_url=f"{RAILWAY_STATIC_URL}/telegram",
        secret_token=WEBHOOK_SECRET
    )
    logger.info("Webhook setup completed")

@web_app.on_event("shutdown")
async def shutdown():
    await application.stop()
    await application.shutdown()
    logger.info("Bot shutdown complete")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await users_col.update_one(
        {"_id": user.id},
        {"$set": {"username": user.username}},
        upsert=True
    )

    if context.args:
        arg = context.args[0]
        if arg.startswith("file_"):
            await handle_file_request(update, context, arg[5:])
        elif arg.startswith("batch_"):
            await handle_batch_request(update, context, arg[6:])
    else:
        if await is_admin(user.id):
            await update.message.reply_text("✅ Admin panel ready\nSend files or use /batch")
        else:
            await update.message.reply_text("❌ You need authorization to use this bot")

async def handle_file_request(update: Update, context: ContextTypes.DEFAULT_TYPE, file_id: str):
    user = update.effective_user
    file_data = await files_col.find_one({"_id": file_id})
    
    if not file_data:
        await update.message.reply_text("⚠️ File not found")
        return

    if await check_membership(user.id, context.bot):
        await context.bot.copy_message(user.id, DUMP_CHANNEL, file_data["message_id"])
    else:
        keyboard = [
            [InlineKeyboardButton(f"Join Channel {i+1}", url=f"t.me/{cid}") 
             for i, cid in enumerate(FORCE_SUBS)],
            [InlineKeyboardButton("✅ Verify Subscription", callback_data=f"verify_{file_id}")]
        ]
        await update.message.reply_text(
            "📢 You must join our channels first!",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data.startswith("verify_"):
        file_id = query.data[7:]
        if await check_membership(query.from_user.id, query.bot):
            file_data = await files_col.find_one({"_id": file_id})
            await query.bot.copy_message(query.from_user.id, DUMP_CHANNEL, file_data["message_id"])
            await query.message.delete()
        else:
            await query.message.edit_text("❌ Still not joined all channels!")

async def store_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not await is_admin(user.id):
        return

    try:
        msg = await update.message.forward(DUMP_CHANNEL)
        file = update.message.document or update.message.photo[-1] if update.message.photo else None
        
        file_id = str(uuid.uuid4())
        await files_col.insert_one({
            "_id": file_id,
            "message_id": msg.message_id,
            "file_id": file.file_id,
            "timestamp": datetime.now(),
            "uploader": user.id
        })
        
        await update.message.reply_text(f"✅ File stored!\n🔗 Permanent Link: {generate_link(file_id)}")
    except Exception as e:
        logger.error(f"File storage error: {e}")
        await update.message.reply_text("❌ Failed to store file")

async def handle_batch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not await is_admin(user.id):
        return

    if not context.user_data.get("batch_mode"):
        context.user_data.update({
            "batch_mode": True,
            "batch_files": [],
            "batch_messages": []
        })
        await update.message.reply_text("📦 Batch mode activated!\nSend files now. Use /endbatch when done")
    else:
        context.user_data["batch_files"].append(update.message)
        await update.message.reply_text("📎 File added to batch")

async def end_batch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not (files := context.user_data.get("batch_files")):
        await update.message.reply_text("⚠️ No files in batch")
        return

    try:
        batch_id = str(uuid.uuid4())
        messages = []
        
        for f in files:
            msg = await f.forward(DUMP_CHANNEL)
            messages.append({
                "message_id": msg.message_id,
                "file_id": f.document.file_id
            })
        
        await batches_col.insert_one({
            "_id": batch_id,
            "files": messages,
            "timestamp": datetime.now()
        })
        
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
            "storage": (await db.command("dbstats"))['dataSize']/1024/1024
        }
        
        stats_msg = (
            "📊 Bot Statistics:\n"
            f"• Users: {stats_data['users']}\n"
            f"• Files: {stats_data['files']}\n"
            f"• Batches: {stats_data['batches']}\n"
            f"• Storage Used: {stats_data['storage']:.2f} MB"
        )
        await update.message.reply_text(stats_msg)
    except Exception as e:
        logger.error(f"Stats error: {e}")
        await update.message.reply_text("❌ Failed to get statistics")

def setup_handlers():
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("batch", handle_batch))
    application.add_handler(CommandHandler("endbatch", end_batch))
    application.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, store_file))
    application.add_handler(CallbackQueryHandler(handle_callback))

if __name__ == "__main__":
    setup_handlers()
    uvicorn.run(
        web_app,
        host="0.0.0.0",
        port=PORT,
        log_level="info",
        timeout_keep_alive=3600
    )
