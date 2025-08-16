import os
import asyncio
import time
import secrets
import uvloop
from multiprocessing import cpu_count
from threading import Thread
from flask import Flask
from dotenv import load_dotenv

from pyrogram import Client, filters, enums
from pyrogram.types import Message
from pyrogram.errors import MessageNotModified

load_dotenv()
uvloop.install()

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMINS = list(map(int, os.getenv("ADMINS", "").split(",")))

web = Flask(__name__)

@web.route('/')
def home():

    return "Bot is running!"

app = Client(
    "TrimBot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workers=min(32, cpu_count() + 4),
    max_concurrent_transmissions=6
    )

video_queue = asyncio.Queue()
active_tasks = {} 

user_settings = {
    "start_time": "00:00:20",
    "end_time": None,
    "caption": None,
    "thumbnail": None
}

@app.on_message(filters.command("start") & filters.private)
async def start(client: Client, message: Message):
    if message.from_user.id not in ADMINS:
        await message.reply_text("You are not authorized to use this bot.")
        return
    await message.reply_text(
        f"Hi {message.from_user.mention}!\n\n"
        "I can trim your videos. Use /help to see available commands."
    )
    return

@app.on_message(filters.command("help") & filters.private)
async def help_command(client: Client, message: Message):
    if message.from_user.id not in ADMINS:
        await message.reply_text("You are not authorized to use this bot.")
        return
    await message.reply_text(
        f"Hi {message.from_user.mention}!\n\n"
        "I can trim your videos. Here's what's new:\n\n"
        "1. **Set Trim Times:** Use `/set_time <start> [end]`.\n"
        "   - The `end` time is optional. If omitted, the video is trimmed to its end.\n"
        "   - *Example:* `/set_time 00:01:10 00:01:50`\n\n"
        "2. **Set Custom Caption:** Use `/set_caption <your_caption>`.\n"
        "   - To revert to the original caption, use `/set_caption` with no text.\n\n"
        "3. **Set Thumbnail:** Reply to an image with `/set_thumbnail` to set a custom thumbnail.\n"
        "   - To remove the custom thumbnail, use `/set_thumbnail` without replying to an image.\n\n"
        "4. **Cancel Operations:** When a video is processing, you'll get a token. Use `/cancel <token>` to stop it.\n\n"
        "5. **Send Video:** Send me the video you want to trim."
    )

@app.on_message(filters.command("set_time") & filters.private & filters.user(ADMINS))
async def set_time(client: Client, message: Message):
    try:
        parts = message.text.split()
        if not (2 <= len(parts) <= 3):
            await message.reply_text("<b>Usage:</b> /set_time <start_time> [end_time]", parse_mode=enums.ParseMode.HTML)
            return

        start_time = parts[1]
        end_time = parts[2] if len(parts) == 3 else None

        if ':' not in start_time or (end_time and ':' not in end_time):
            await message.reply_text("Invalid time format. Please use HH:MM:SS or MM:SS format.")
            return

        user_settings["start_time"] = start_time
        user_settings["end_time"] = end_time

        end_display = end_time if end_time else "End of Video"
        await message.reply_text(
            f"✅ Trim time successfully set!\n<b>Start:</b> {start_time}\n<b>End:</b> {end_display}",
            parse_mode=enums.ParseMode.HTML
        )
    except Exception as e:
        await message.reply_text(f"An error occurred: {e}")

@app.on_message(filters.command("set_caption") & filters.private & filters.user(ADMINS))
async def set_caption(client: Client, message: Message):
    try:
        caption = message.text.split(" ", 1)[1]
        user_settings["caption"] = caption
        await message.reply_text(f"✅ Custom caption has been set to:\n\n{caption}")
    except IndexError:
        user_settings["caption"] = None
        await message.reply_text("✅ Custom caption removed. The original video's caption will be used.")

@app.on_message(filters.command("set_thumbnail") & filters.private & filters.user(ADMINS))
async def set_thumbnail(client: Client, message: Message):
    if message.reply_to_message and message.reply_to_message.photo:
        user_settings["thumbnail"] = message.reply_to_message.photo.file_id
        await message.reply_text("✅ Custom thumbnail has been set.")
    else:
        user_settings["thumbnail"] = None
        await message.reply_text("✅ Custom thumbnail removed.")

@app.on_message(filters.command("cancel") & filters.private & filters.user(ADMINS))
async def cancel_task(client: Client, message: Message):
    try:
        token_to_cancel = message.text.split(" ", 1)[1]
        if token_to_cancel in active_tasks:
            task_to_cancel = active_tasks[token_to_cancel]
            task_to_cancel.cancel()
        else:
            await message.reply_text("❌ Invalid token or task is already completed.")
    except IndexError:
        await message.reply_text("<b>Usage:</b> /cancel <token>", parse_mode=enums.ParseMode.HTML)
    except Exception as e:
        await message.reply_text(f"An error occurred during cancellation: {e}")

@app.on_message(filters.video & filters.private & filters.user(ADMINS))
async def add_video_to_queue(client: Client, message: Message):
    if not message.video:
        await message.reply_text("Please send a valid video file.")
        return

    await video_queue.put(message)
    await message.reply_text(
        "☑️ Your video has been added to the queue and will be processed shortly.",
        quote=True
    )

async def process_video_queue(client: Client):
    while True:
        message = await video_queue.get()
        token = secrets.token_hex(3) # Generates a 6-character hex token

        status_message = await message.reply_text(f"Initializing...", quote=True)

        download_dir = "downloads"
        if not os.path.isdir(download_dir):
            os.makedirs(download_dir)

        file_name = message.video.file_name or f"video_{int(time.time())}.mp4"
        download_path = os.path.join(download_dir, file_name)
        trimmed_path = os.path.join(download_dir, f"trimmed_{file_name}")
        thumbnail_path = None

        task = asyncio.current_task()
        active_tasks[token] = task

        try:
            last_update_time = 0
            last_bytes = 0
            last_time = time.time()
            async def progress(current, total, action):
                nonlocal last_update_time, last_bytes, last_time
                now = time.time()
                elapsed = now - last_time
                bytes_diff = current - last_bytes
                speed = 0
                if elapsed > 0:
                    speed = bytes_diff / elapsed / (1024 * 1024)  # MB/s
                if now - last_update_time > 5:
                    percentage = (current * 100) / total
                    try:
                        await status_message.edit(
                            f"**{action}...**\n"
                            f"`[{'◉' * int(percentage / 10)}{'○' * (10 - int(percentage / 10))}] {percentage:.1f}%`\n"
                            f"Speed: {speed:.2f} MB/s\n\n"
                            f"To cancel, use: `/cancel {token}`"
                        )
                    except MessageNotModified:
                        pass
                    last_update_time = now
                    last_bytes = current
                    last_time = now

            # 1. Download Video
            await client.download_media(message, file_name=download_path, progress=progress, progress_args=("Downloading 📥",))

            # 2. Trim Video
            await status_message.edit(f"✂️ Trimming video...")
            start = user_settings["start_time"]
            end = user_settings["end_time"]
            command = ['ffmpeg', '-i', download_path, '-ss', start, '-c:v', 'copy', '-c:a', 'copy']
            if end:
                command.extend(['-to', end])
            command.append(trimmed_path)

            process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            _, stderr = await process.communicate()

            if process.returncode != 0:
                await status_message.edit(f"❌ Failed to trim video.\n\n**Error:**\n`{stderr.decode().strip()}`")
                continue

            # Download thumbnail if set
            if user_settings["thumbnail"]:
                await status_message.edit("🖼️ Downloading thumbnail...")
                thumbnail_path = await client.download_media(user_settings["thumbnail"], file_name=os.path.join(download_dir, "thumbnail.jpg"))

            # 3. Upload Video
            final_caption = user_settings["caption"] if user_settings["caption"] is not None else message.caption
            await client.send_video(
                chat_id=message.chat.id,
                video=trimmed_path,
                caption=final_caption,
                thumb=thumbnail_path,
                reply_to_message_id=message.id,
                progress=progress,
                progress_args=("Uploading 📤",)
            )
            await status_message.delete()

        except asyncio.CancelledError:
            await status_message.edit(f"✅ Operation cancelled by user (`{token}`).")
        except Exception as e:
            await status_message.edit(f"❌ An unexpected error occurred: {e}")
        finally:
            if os.path.exists(download_path):
                os.remove(download_path)
            if os.path.exists(trimmed_path):
                os.remove(trimmed_path)
            if thumbnail_path and os.path.exists(thumbnail_path):
                os.remove(thumbnail_path)

            if token in active_tasks:
                del active_tasks[token]

            video_queue.task_done()

async def main():
    flask_thread = Thread(target=lambda: web.run(host='0.0.0.0', port=int(os.getenv("PORT", 8080))), daemon=True)
    flask_thread.start()

    await app.start()
    print("✅ Bot has started successfully!")

    asyncio.create_task(process_video_queue(client=app))
    await asyncio.Event().wait()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        print("Bot is shutting down...")
