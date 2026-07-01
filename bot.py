import logging

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import BotCommand, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

import config
import database as db
from scraper import fetch_channel_posts

logger = logging.getLogger(__name__)

bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# 1. Define states for both CV upload and channel registration processes
class JobBotStates(StatesGroup):
    waiting_for_cv = State()
    waiting_for_channel = State()

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await db.register_or_get_user(message.from_user.id)
    await message.answer("Welcome! Use /help to see available commands to manage your CV and job channels.")

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    help_text = (
        "🧠 **Available Commands:**\n\n"
        "/cv - Update your CV (Paste text/LaTeX or upload a .txt/.tex/.md file)\n"
        "/add_channel - Add a public Telegram channel to monitor\n"
        "/rm_channel - Remove a monitored channel via menu\n"
        "/threshold - Check your current matching percentage\n"
        "/set_threshold - Change matching percentage configuration\n"
        "/list - Show your registered channels\n"
        "/stats - Show a summary of your configuration\n"
        "/cancel - Abort your current active action"
    )
    await message.answer(help_text)

# --- CV Ingestion Flow ---

@dp.message(Command("cv"))
async def cmd_cv(message: types.Message, state: FSMContext):
    await state.set_state(JobBotStates.waiting_for_cv)
    await message.answer("Please send your CV now. You can paste your text/LaTeX code directly or upload a `.txt`, `.tex`, or `.md` file.")

@dp.message(JobBotStates.waiting_for_cv, F.document | F.text)
async def handle_cv_upload(message: types.Message, state: FSMContext):
    if message.document:
        cv_text = await _read_cv_document(message)
        if cv_text is None:
            await state.clear()  # error already reported to the user
            return
    else:
        cv_text = message.text

    if not cv_text or not cv_text.strip():
        await message.answer("❌ That looked empty. Run /cv and send your CV text or a file.")
        await state.clear()
        return

    await db.update_user_cv(message.from_user.id, cv_text)
    await message.answer("✅ CV updated successfully!")
    await state.clear()

@dp.message(JobBotStates.waiting_for_cv)
async def handle_cv_invalid(message: types.Message):
    # Catches photos/stickers/etc. so the user isn't silently stuck in-state.
    await message.answer("❌ Please paste text or upload a .txt/.tex/.md file, or /cancel.")

async def _read_cv_document(message: types.Message) -> str | None:
    """Download a CV document into memory and validate it. Returns text or None
    (after reporting a specific error to the user)."""
    doc = message.document
    filename = (doc.file_name or "").lower()

    if doc.file_size and doc.file_size > config.MAX_CV_BYTES:
        limit_kb = config.MAX_CV_BYTES // 1024
        await message.answer(f"❌ That file is too large (limit {limit_kb} KB).")
        return None

    if filename and not filename.endswith(config.ALLOWED_CV_EXTENSIONS):
        allowed = ", ".join(config.ALLOWED_CV_EXTENSIONS)
        await message.answer(f"❌ Unsupported file type. Please upload one of: {allowed}.")
        return None

    try:
        file = await bot.get_file(doc.file_id)
        buffer = await bot.download_file(file.file_path)  # BytesIO, no disk
        return buffer.getvalue().decode("utf-8", errors="replace")
    except Exception as e:
        logger.error(f"CV download failed for user {message.from_user.id}: {e}")
        await message.answer("❌ Could not read that file. Please try again with /cv.")
        return None

# --- Add Channel Flow ---

@dp.message(Command("add_channel"))
async def cmd_add_start(message: types.Message, state: FSMContext):
    await state.set_state(JobBotStates.waiting_for_channel)
    await message.answer(
        "Please send the channel username or link\n"
        "(e.g., `https://t.me/remotegeekjob` or `@remotegeekjob`):",
        parse_mode="Markdown"
    )

@dp.message(JobBotStates.waiting_for_channel)
async def process_channel_input(message: types.Message, state: FSMContext):
    if not message.text:
        return await message.answer("❌ Error: Please send text. Media files are not accepted here.")

    channel_input = config.normalize_channel(message.text)
    if not channel_input:
        await message.answer("❌ That doesn't look like a valid channel name or link.")
        await state.clear()
        return

    # Verify the channel is reachable and has public posts before saving.
    await message.answer(f"🔎 Checking t.me/{channel_input} …")
    posts = await fetch_channel_posts(channel_input)
    if not posts:
        await message.answer(
            f"❌ Couldn't find public posts for t.me/{channel_input}. "
            "Check the name — it must be a public channel."
        )
        await state.clear()
        return

    success = await db.add_subscription(message.from_user.id, channel_input)
    if success:
        await message.answer(f"✅ Successfully subscribed to t.me/{channel_input}")
    else:
        await message.answer("❌ You are already subscribed to that channel.")

    await state.clear()

# --- Global Settings & Control Flow ---

@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Action cancelled.")

@dp.message(Command("threshold"))
async def cmd_threshold(message: types.Message):
    user_data = await db.register_or_get_user(message.from_user.id)
    # register_or_get_user returns tuple: (cv_text, match_threshold)
    current_threshold = user_data[1] if user_data else config.DEFAULT_THRESHOLD
    await message.answer(f"📊 Your current match requirement threshold is: **{current_threshold}%**")

@dp.message(Command("set_threshold"))
async def cmd_set_threshold_menu(message: types.Message):
    thresholds = list(range(config.THRESHOLD_MIN, config.THRESHOLD_MAX + 1, config.THRESHOLD_STEP))
    buttons = []
    row = []

    for val in thresholds:
        row.append(InlineKeyboardButton(text=f"{val}%", callback_data=f"th_{val}"))
        if len(row) == 4:  # Balanced 4-per-row layout
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer("Select your desired matching threshold level below:", reply_markup=keyboard)

@dp.callback_query(F.data.startswith("th_"))
async def process_threshold_callback(callback: CallbackQuery):
    val = int(callback.data.removeprefix("th_"))
    await db.update_user_threshold(callback.from_user.id, val)
    await callback.message.edit_text(f"✅ Matching requirement threshold updated to **{val}%**")
    await callback.answer()

@dp.message(Command("list"))
async def cmd_list(message: types.Message):
    subs = await db.get_user_subscriptions(message.from_user.id)
    if not subs:
        await message.answer("No channels configured yet.")
        return
    lines = "\n".join(f"• t.me/{ch}" for ch in subs)
    await message.answer(f"Your channels:\n{lines}")

@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    stats = await db.get_user_stats(message.from_user.id)
    cv_line = f"set ({stats['cv_length']} chars)" if stats["has_cv"] else "not set"
    await message.answer(
        "📈 **Your configuration**\n\n"
        f"• Channels tracked: {stats['channel_count']}\n"
        f"• CV: {cv_line}\n"
        f"• Match threshold: {stats['threshold']}%"
    )

# --- Channel Removal Flow ---

@dp.message(Command("rm_channel"))
async def cmd_rm_menu(message: types.Message):
    subs = await db.get_user_subscriptions(message.from_user.id)
    if not subs:
        return await message.answer("You are not tracking any channels.")

    # Telegram usernames are <=32 chars, so "rm_<channel>" stays well under the
    # 64-byte callback_data limit; keeping the name avoids index staleness.
    buttons = [
        [InlineKeyboardButton(text=f"❌ {ch}", callback_data=f"rm_{ch}")]
        for ch in subs
    ]
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer("Select a channel to remove:", reply_markup=keyboard)

@dp.callback_query(F.data.startswith("rm_"))
async def process_rm_callback(callback: CallbackQuery):
    channel = callback.data.removeprefix("rm_")
    await db.remove_subscription(callback.from_user.id, channel)
    await callback.message.edit_text(f"✅ Removed {channel} from your tracking list.")
    await callback.answer()

# --- Runtime Launch Orchestrator ---

async def start_bot():
    await db.init_db()

    commands = [
        BotCommand(command="start", description="Initialize/Start bot context"),
        BotCommand(command="cv", description="Update your CV profile data"),
        BotCommand(command="add_channel", description="Add a target monitoring channel"),
        BotCommand(command="rm_channel", description="Open channel deletion interface"),
        BotCommand(command="threshold", description="View current matching settings"),
        BotCommand(command="set_threshold", description="Configure match requirements"),
        BotCommand(command="list", description="Show all tracked channels"),
        BotCommand(command="stats", description="Show a summary of your configuration"),
        BotCommand(command="help", description="Show application commands manual"),
    ]
    await bot.set_my_commands(commands)
    await dp.start_polling(bot)
