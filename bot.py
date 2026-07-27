import logging
import math

from aiogram import Bot, Dispatcher, types, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.types import BotCommand, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

import config
import database as db
from scraper import fetch_channel_posts
from ai_engine import extract_skills_from_cv, _normalize_skills

logger = logging.getLogger(__name__)

bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# 1. Define states for the CV, channel, and skill entry processes
class JobBotStates(StatesGroup):
    waiting_for_cv = State()
    waiting_for_channel = State()
    waiting_for_skill = State()

# Every multi-step flow, used by the command guard below.
_IN_FLOW = StateFilter(
    JobBotStates.waiting_for_cv,
    JobBotStates.waiting_for_channel,
    JobBotStates.waiting_for_skill,
)

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await db.register_or_get_user(message.from_user.id)
    await message.answer("Welcome! Use /help to see available commands to manage your CV and job channels.")

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    help_text = (
        "🧠 **Available Commands:**\n\n"
        "/cv - Update your CV (Paste text/LaTeX or upload a .txt/.tex/.md file)\n"
        "/skills - Review the skills used for matching (add/remove/star must-haves)\n"
        "/workmode - Filter by remote / on-site / hybrid\n"
        "/add_channel - Add a public Telegram channel to monitor\n"
        "/rm_channel - Remove a monitored channel via menu\n"
        "/threshold - Check your current matching percentage\n"
        "/set_threshold - Change matching percentage configuration\n"
        "/list - Show your registered channels\n"
        "/stats - Show a summary of your configuration\n"
        "/cancel - Abort your current active action"
    )
    await message.answer(help_text)

# --- In-flow command guard ---------------------------------------------------
# MUST be registered before the state content handlers below: aiogram dispatches
# in registration order, so without this a "/cancel" typed during /cv would be
# matched by handle_cv_upload and saved as the CV text itself.

@dp.message(_IN_FLOW, F.text & F.text.startswith("/"))
async def handle_command_during_flow(message: types.Message, state: FSMContext):
    command = message.text.split()[0].lstrip("/").split("@")[0].lower()
    if command == "cancel":
        await state.clear()
        await message.answer("Action cancelled.")
    else:
        await message.answer("⚠️ Please finish this step first, or send /cancel to abort.")

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

    # Save the CV first: it is the critical path, and Groq is not.
    await db.update_user_cv(message.from_user.id, cv_text)
    await state.clear()
    # Sent before the (slow) extraction call so the chat isn't left silent.
    await message.answer("✅ CV updated successfully!\n\n🧠 Extracting your skills… this takes a few seconds.")

    await _extract_and_report_skills(message, cv_text)

@dp.message(JobBotStates.waiting_for_cv)
async def handle_cv_invalid(message: types.Message):
    # Catches photos/stickers/etc. so the user isn't silently stuck in-state.
    await message.answer("❌ Please paste text or upload a .txt/.tex/.md file, or /cancel.")

async def _extract_and_report_skills(message: types.Message, cv_text: str):
    """Auto-extract skills from a CV and merge them into the user's list."""
    try:
        skills = await extract_skills_from_cv(cv_text)
    except Exception as e:
        logger.error(f"Skill extraction crashed for {message.from_user.id}: {e}")
        skills = []

    if not skills:
        await message.answer(
            "⚠️ Couldn't auto-extract your skills. You can add them manually with /skills."
        )
        return

    # Merge, never replace: this preserves hand-curated skills and their
    # must-have stars, so re-uploading a CV can't silently change filtering.
    added = await db.add_user_skills(message.from_user.id, skills)
    already = len(skills) - added
    await message.answer(
        f"🛠 Found {len(skills)} skills:\n{', '.join(skills)}\n\n"
        f"Added {added} new · {already} already on your list.\n"
        "Use /skills to add, remove, or star must-haves."
    )

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

# --- Work Mode Filter ---

@dp.message(Command("workmode"))
async def cmd_workmode(message: types.Message):
    await db.register_or_get_user(message.from_user.id)
    current = await db.get_user_work_mode(message.from_user.id)
    await message.answer(
        f"Current work mode filter: <b>{config.WORK_MODE_LABELS.get(current, current)}</b>\n\n"
        "Jobs that clearly don't match this are never sent to you. "
        "Posts that don't state their work mode always come through.",
        reply_markup=_workmode_keyboard(),
        parse_mode="HTML",
    )

def _workmode_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(
            text=config.WORK_MODE_LABELS[mode], callback_data=f"wm_{mode}"
        )]
        for mode in config.WORK_MODE_PREFS
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

@dp.callback_query(F.data.startswith("wm_"))
async def process_workmode_callback(callback: CallbackQuery):
    mode = callback.data.removeprefix("wm_")
    if mode not in config.WORK_MODE_PREFS:
        await callback.answer("Unknown option.", show_alert=True)
        return
    await db.set_user_work_mode(callback.from_user.id, mode)
    await callback.message.edit_text(
        f"✅ Work mode filter set to <b>{config.WORK_MODE_LABELS[mode]}</b>",
        parse_mode="HTML",
    )
    await callback.answer()

# --- Skills Filter ---

def _skills_keyboard(rows: list, page: int, has_cv: bool = False) -> InlineKeyboardMarkup:
    """Build the /skills keyboard for one page.

    callback_data carries row ids, never skill names, so it stays far under
    Telegram's 64-byte limit no matter how long a skill is.
    """
    per_page = config.SKILLS_PAGE_SIZE
    total_pages = max(1, math.ceil(len(rows) / per_page))
    page = max(0, min(page, total_pages - 1))
    chunk = rows[page * per_page:(page + 1) * per_page]

    buttons = []
    for skill_id, skill, must_have in chunk:
        star = "⭐" if must_have else "☆"
        buttons.append([
            InlineKeyboardButton(text=f"{star} {skill}", callback_data=f"sk_t_{skill_id}"),
            InlineKeyboardButton(text="🗑", callback_data=f"sk_d_{skill_id}"),
        ])

    if total_pages > 1:
        buttons.append([
            InlineKeyboardButton(
                text="‹", callback_data=f"sk_p_{page - 1}" if page > 0 else "sk_nop"
            ),
            InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="sk_nop"),
            InlineKeyboardButton(
                text="›",
                callback_data=f"sk_p_{page + 1}" if page < total_pages - 1 else "sk_nop",
            ),
        ])

    bottom = [InlineKeyboardButton(text="➕ Add", callback_data="sk_add")]
    if rows:
        bottom.append(InlineKeyboardButton(text="🧹 Clear all", callback_data="sk_clr"))
    elif has_cv:
        bottom.append(InlineKeyboardButton(text="🔎 Extract from my CV", callback_data="sk_ext"))
    buttons.append(bottom)

    return InlineKeyboardMarkup(inline_keyboard=buttons)

async def _render_skills(user_id: int, page: int = 0):
    """Returns (text, markup) for the skills screen — shared by command and callbacks."""
    rows = await db.get_user_skills(user_id)
    user = await db.register_or_get_user(user_id)
    has_cv = bool(user and user[0])

    if not rows:
        text = (
            "🛠 <b>Your skills</b>\n\nYou have no skills saved yet.\n\n"
            "Skills sharpen how jobs are scored. Add them manually, or extract "
            "them from your CV."
        )
    else:
        must_count = sum(1 for _, _, m in rows if m)
        text = (
            f"🛠 <b>Your skills</b> ({len(rows)}/{config.MAX_SKILLS}"
            f"{f' · {must_count} must-have' if must_count else ''})\n\n"
            "Tap a skill to toggle ⭐ <b>must-have</b> (a job must mention at least "
            "one of your starred skills). Tap 🗑 to remove it."
        )
    return text, _skills_keyboard(rows, page, has_cv=has_cv)

@dp.message(Command("skills"))
async def cmd_skills(message: types.Message):
    await db.register_or_get_user(message.from_user.id)
    text, markup = await _render_skills(message.from_user.id)
    await message.answer(text, reply_markup=markup, parse_mode="HTML")

async def _refresh_skills(callback: CallbackQuery, page: int = 0):
    """Re-render the skills screen in place, tolerating no-op edits."""
    text, markup = await _render_skills(callback.from_user.id, page)
    try:
        await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except TelegramBadRequest:
        pass  # "message is not modified" — harmless

@dp.callback_query(F.data == "sk_nop")
async def skills_noop(callback: CallbackQuery):
    await callback.answer()

@dp.callback_query(F.data.startswith("sk_p_"))
async def skills_page(callback: CallbackQuery):
    try:
        page = int(callback.data.removeprefix("sk_p_"))
    except ValueError:
        page = 0
    await _refresh_skills(callback, page)
    await callback.answer()

@dp.callback_query(F.data.startswith("sk_t_"))
async def skills_toggle(callback: CallbackQuery):
    try:
        skill_id = int(callback.data.removeprefix("sk_t_"))
    except ValueError:
        return await callback.answer()
    result = await db.toggle_skill_must_have(callback.from_user.id, skill_id)
    if result is None:
        await callback.answer("That skill is gone.", show_alert=True)
    else:
        await callback.answer("⭐ Must-have" if result else "☆ Optional")
    await _refresh_skills(callback)

@dp.callback_query(F.data.startswith("sk_d_"))
async def skills_delete(callback: CallbackQuery):
    try:
        skill_id = int(callback.data.removeprefix("sk_d_"))
    except ValueError:
        return await callback.answer()
    removed = await db.delete_user_skill(callback.from_user.id, skill_id)
    await callback.answer("Removed" if removed else "That skill is gone.")
    await _refresh_skills(callback)

@dp.callback_query(F.data == "sk_clr")
async def skills_clear_confirm(callback: CallbackQuery):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Yes, clear all", callback_data="sk_clrY"),
        InlineKeyboardButton(text="↩️ Keep them", callback_data="sk_p_0"),
    ]])
    try:
        await callback.message.edit_text(
            "🧹 Remove <b>all</b> of your saved skills?", reply_markup=keyboard, parse_mode="HTML"
        )
    except TelegramBadRequest:
        pass
    await callback.answer()

@dp.callback_query(F.data == "sk_clrY")
async def skills_clear(callback: CallbackQuery):
    removed = await db.clear_user_skills(callback.from_user.id)
    await callback.answer(f"Cleared {removed}.")
    await _refresh_skills(callback)

@dp.callback_query(F.data == "sk_ext")
async def skills_extract(callback: CallbackQuery):
    user = await db.register_or_get_user(callback.from_user.id)
    cv_text = user[0] if user else None
    if not cv_text:
        return await callback.answer("Upload a CV first with /cv.", show_alert=True)

    await callback.answer("Extracting…")
    try:
        skills = await extract_skills_from_cv(cv_text)
    except Exception as e:
        logger.error(f"Skill extraction crashed for {callback.from_user.id}: {e}")
        skills = []

    if skills:
        await db.add_user_skills(callback.from_user.id, skills)
    else:
        await callback.message.answer("⚠️ Couldn't extract skills. Add them manually with ➕ Add.")
    await _refresh_skills(callback)

@dp.callback_query(F.data == "sk_add")
async def skills_add_prompt(callback: CallbackQuery, state: FSMContext):
    await state.set_state(JobBotStates.waiting_for_skill)
    await callback.message.answer(
        "Send the skill(s) to add. Separate several with commas, e.g.\n"
        "`Python, Docker, PostgreSQL`\n\nOr /cancel to abort.",
        parse_mode="Markdown",
    )
    await callback.answer()

@dp.message(JobBotStates.waiting_for_skill, F.text)
async def handle_skill_input(message: types.Message, state: FSMContext):
    candidates = _normalize_skills([part for part in message.text.split(",")])
    await state.clear()

    if not candidates:
        await message.answer("❌ Nothing usable there. Try again with /skills.")
        return

    before = await db.count_user_skills(message.from_user.id)
    added = await db.add_user_skills(message.from_user.id, candidates)
    after = await db.count_user_skills(message.from_user.id)

    parts = [f"✅ Added {added}."]
    if added < len(candidates):
        skipped = len(candidates) - added
        if after >= config.MAX_SKILLS and after > before:
            parts.append(f"{skipped} skipped (limit of {config.MAX_SKILLS} reached).")
        else:
            parts.append(f"{skipped} already on your list.")
    await message.answer(" ".join(parts))

    text, markup = await _render_skills(message.from_user.id)
    await message.answer(text, reply_markup=markup, parse_mode="HTML")

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
    mode = stats["work_mode"]
    skills_line = f"{stats['skill_count']}"
    if stats["must_have_count"]:
        skills_line += f" ({stats['must_have_count']} must-have)"
    await message.answer(
        "📈 **Your configuration**\n\n"
        f"• Channels tracked: {stats['channel_count']}\n"
        f"• CV: {cv_line}\n"
        f"• Skills: {skills_line}\n"
        f"• Work mode: {config.WORK_MODE_LABELS.get(mode, mode)}\n"
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
    await db.init_db()  # idempotent; main() also calls it before the monitor starts

    commands = [
        BotCommand(command="start", description="Initialize/Start bot context"),
        BotCommand(command="cv", description="Update your CV profile data"),
        BotCommand(command="skills", description="Manage the skills used for matching"),
        BotCommand(command="workmode", description="Filter by remote / on-site / hybrid"),
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
