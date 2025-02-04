import logging
import os
from pymongo import MongoClient
from telegram import Update
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# Load environment variables
ADMIN_IDS = list(map(int, os.getenv('ADMIN_IDS', '').split(',')))
DUMP_CHANNEL = os.getenv('DUMP_CHANNEL')
FORCE_CHANNELS = list(map(str, os.getenv('FORCE_CHANNELS', '').split(',')))

# MongoDB connection
mongo_uri = os.getenv('MONGO_URI')  # Your MongoDB URI
client = MongoClient(mongo_uri)
db = client['telegram_bot_db']  # Your database name
users_collection = db['users']  # Collection for user data
files_collection = db['files']  # Collection for files data

def start(update: Update, context: CallbackContext) -> None:
    user_id = update.effective_user.id
    user_name = update.effective_user.first_name
    
    # Register or update user in the database
    users_collection.update_one({'user_id': user_id}, {'$set': {'username': user_name}}, upsert=True)

    update.message.reply_text(f'Hello {user_name}! Welcome to the bot.')

def check_subscription(user_id):
    for channel in FORCE_CHANNELS:
        member = context.bot.get_chat_member(channel, user_id)
        if member.status not in ['member', 'administrator']:
            return False
    return True

def upload_file(update: Update, context: CallbackContext) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        update.message.reply_text("You do not have permission to upload files.")
        return
    
    if update.message.document:
        document = update.message.document
        file_id = document.file_id
        file_name = document.file_name

        # Send the file to the dump channel
        context.bot.send_document(chat_id=DUMP_CHANNEL, document=document)

        # Store file info in the database
        files_collection.insert_one({
            'file_id': file_id,
            'file_name': file_name,
            'uploaded_by': update.effective_user.id
        })

        # Generate a permanent link here (you may need to implement this)
        link = f"https://your-heroku-app.herokuapp.com/file/{file_id}"  # Example link generation

        update.message.reply_text(f"File uploaded successfully! Here's the link: {link}")

def broadcast_message(update: Update, context: CallbackContext) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        update.message.reply_text("You do not have permission to broadcast messages.")
        return

    message = " ".join(context.args)
    users = users_collection.find()
    for user in users:
        context.bot.send_message(chat_id=user['user_id'], text=message)

def view_stats(update: Update, context: CallbackContext) -> None:
    total_users = users_collection.count_documents({})
    total_files = files_collection.count_documents({})
    
    update.message.reply_text(f"Total Users: {total_users}\nTotal Files Uploaded: {total_files}")

def main():
    updater = Updater("YOUR_TOKEN")  # Replace with your bot token
    dp = updater.dispatcher

    # Register handlers
    dp.add_handler(CommandHandler('start', start))
    dp.add_handler(MessageHandler(Filters.document, upload_file))
    dp.add_handler(CommandHandler('broadcast', broadcast_message))
    dp.add_handler(CommandHandler('stats', view_stats))  # Command to show stats

    # Start polling for updates
    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    main()
