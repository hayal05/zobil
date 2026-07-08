"""
Digital Store Telegram Bot (Books / Movies / Music)
-----------------------------------------------------
Production-ready single-file bot built with python-telegram-bot v20+.

Features:
- Browse products by category (Books / Movies / Music) via inline keyboards.
- View product details (title, category, description, cover photo, price) with a "Buy Now" button.
- Manual payment flow (Telebirr / CBE) with screenshot upload for verification.
- Cancel button on the checkout flow, plus a /myorders command for buyers to
  check the status of their past orders.
- Admin approval/rejection of orders. On approval, the Google Drive delivery link is
  automatically sent to the buyer. On rejection, the buyer is notified.
- Full admin panel (/admin) restricted to ADMIN_ID:
    - Add product (category, title, price, description, photo, Google Drive link)
    - Remove product
    - Edit price
    - Edit Telebirr / CBE payment details
    - List products
- Conversations automatically time out after 10 minutes of inactivity so a user
  can never get permanently "stuck" mid-flow.
- Errors are logged AND sent to the admin in Telegram, so a bug never just
  looks like "the button did nothing."
- SQLite persistence (products, payment_settings, orders) created automatically on startup.
- Built-in aiohttp web server bound to $PORT so Render's health check passes and the
  service does not get killed for "no open ports".

Environment variables required:
    TELEGRAM_BOT_TOKEN  - the bot token from @BotFather
    ADMIN_ID            - your numeric Telegram user id

Optional:
    PORT        - defaults to 8080 (Render sets this automatically)
    DB_FILE     - defaults to "store.db"
    STORE_NAME  - defaults to "Digital Store"
    CURRENCY    - defaults to "ETB"
"""

import os
import re
import json
import html
import logging
import sqlite3
from datetime import datetime

from aiohttp import web

from google.oauth2 import service_account
from google.oauth2.credentials import Credentials as GoogleUserCredentials
from googleapiclient.discovery import build as build_drive_service
from googleapiclient.errors import HttpError

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    MenuButtonCommands,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    TypeHandler,
    filters,
)

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN environment variable is not set.")

_raw_admin_id = os.environ.get("ADMIN_ID")
if not _raw_admin_id:
    raise RuntimeError("ADMIN_ID environment variable is not set.")
try:
    ADMIN_ID = int(_raw_admin_id)
except ValueError:
    raise RuntimeError("ADMIN_ID must be a numeric Telegram user id.")

PORT = int(os.environ.get("PORT", 8080))
DB_FILE = os.environ.get("DB_FILE", "store.db")
STORE_NAME = os.environ.get("STORE_NAME", "Digital Store")
CURRENCY = os.environ.get("CURRENCY", "ETB")

# Google Drive service account, used to grant per-buyer (email-restricted)
# access to product files instead of relying on "anyone with the link".
# Provide EITHER the raw JSON key content (handy for platforms like Render
# where you can only set env vars, not upload files) OR a path to the key
# file on disk.
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
GOOGLE_SERVICE_ACCOUNT_FILE = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")

# Alternative to a service account: an OAuth refresh token for a *regular*
# Google account (the account that actually owns/has your Drive storage).
# Get these from the OAuth client you created for this app (OAuth consent
# screen -> Credentials -> OAuth client ID, then run the OAuth flow once to
# obtain a refresh token with the drive scope).
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
GOOGLE_REFRESH_TOKEN = os.environ.get("GOOGLE_REFRESH_TOKEN")
GOOGLE_TOKEN_URI = os.environ.get("GOOGLE_TOKEN_URI", "https://oauth2.googleapis.com/token")

CATEGORIES = ["Books", "Movies", "Music"]
CATEGORY_EMOJI = {"Books": "📚", "Movies": "🎬", "Music": "🎵"}

CONVERSATION_TIMEOUT_SECONDS = 600  # 10 minutes

# --------------------------------------------------------------------------- #
# Database helpers
# --------------------------------------------------------------------------- #


def get_conn():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            price REAL NOT NULL,
            description TEXT,
            photo_file_id TEXT,
            drive_file_id TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS payment_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            telebirr TEXT DEFAULT 'Not configured yet.',
            cbe TEXT DEFAULT 'Not configured yet.'
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            product_id INTEGER NOT NULL,
            buyer_email TEXT,
            screenshot_file_id TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT
        )
        """
    )
    cur.execute(
        "INSERT OR IGNORE INTO payment_settings (id, telebirr, cbe) VALUES (1, ?, ?)",
        ("Not configured yet.", "Not configured yet."),
    )
    conn.commit()

    # --- Lightweight migrations for databases created by older versions --- #
    # CREATE TABLE IF NOT EXISTS does nothing if the table already exists with
    # an older schema, so existing installs need these columns added by hand.
    def _ensure_column(table, column, coldef):
        cur.execute(f"PRAGMA table_info({table})")
        existing = {row[1] for row in cur.fetchall()}
        if column not in existing:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coldef}")
            conn.commit()
            logger.info("Migrated database: added %s.%s", table, column)

    _ensure_column("orders", "buyer_email", "TEXT")
    _ensure_column("products", "drive_file_id", "TEXT")
    # Older installs had a NOT NULL "drive_link" column holding a public link.
    # If it's still present, backfill drive_file_id from it where possible so
    # existing products keep working; admins should still re-share those
    # files with the service account and swap to a real File ID over time.
    cur.execute("PRAGMA table_info(products)")
    product_cols = {row[1] for row in cur.fetchall()}
    if "drive_link" in product_cols:
        cur.execute(
            "UPDATE products SET drive_file_id = drive_link "
            "WHERE (drive_file_id IS NULL OR drive_file_id = '') AND drive_link IS NOT NULL"
        )
        conn.commit()

    conn.close()


def db_execute(query, params=(), fetchone=False, fetchall=False, commit=False):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(query, params)
        result = None
        if fetchone:
            result = cur.fetchone()
        elif fetchall:
            result = cur.fetchall()
        if commit:
            conn.commit()
            result = cur.lastrowid
        return result
    finally:
        conn.close()


def add_product(category, title, price, description, photo_file_id, drive_file_id):
    return db_execute(
        """INSERT INTO products (category, title, price, description, photo_file_id, drive_file_id)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (category, title, price, description, photo_file_id, drive_file_id),
        commit=True,
    )


def get_products_by_category(category):
    return db_execute(
        "SELECT * FROM products WHERE category = ? ORDER BY id DESC", (category,), fetchall=True
    )


def get_product(product_id):
    return db_execute("SELECT * FROM products WHERE id = ?", (product_id,), fetchone=True)


def get_all_products():
    return db_execute("SELECT * FROM products ORDER BY category, id", fetchall=True)


def delete_product(product_id):
    db_execute("DELETE FROM products WHERE id = ?", (product_id,), commit=True)


def update_product_price(product_id, new_price):
    db_execute("UPDATE products SET price = ? WHERE id = ?", (new_price, product_id), commit=True)


def get_payment_settings():
    return db_execute("SELECT * FROM payment_settings WHERE id = 1", fetchone=True)


def update_payment_setting(method, text):
    if method == "telebirr":
        db_execute("UPDATE payment_settings SET telebirr = ? WHERE id = 1", (text,), commit=True)
    elif method == "cbe":
        db_execute("UPDATE payment_settings SET cbe = ? WHERE id = 1", (text,), commit=True)


def create_order(user_id, username, product_id, buyer_email, screenshot_file_id):
    return db_execute(
        """INSERT INTO orders (user_id, username, product_id, buyer_email, screenshot_file_id, status, created_at)
           VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
        (user_id, username, product_id, buyer_email, screenshot_file_id, datetime.utcnow().isoformat()),
        commit=True,
    )


def get_order(order_id):
    return db_execute("SELECT * FROM orders WHERE id = ?", (order_id,), fetchone=True)


def update_order_status(order_id, status):
    db_execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id), commit=True)


def get_orders_by_user(user_id, limit=20):
    return db_execute(
        """SELECT orders.*, products.title AS product_title, products.price AS product_price
           FROM orders
           LEFT JOIN products ON orders.product_id = products.id
           WHERE orders.user_id = ?
           ORDER BY orders.id DESC
           LIMIT ?""",
        (user_id, limit),
        fetchall=True,
    )


# --------------------------------------------------------------------------- #
# Google Drive integration
# --------------------------------------------------------------------------- #
#
# Products are delivered as *restricted* Drive files: instead of sharing
# "anyone with the link", the buyer's own email address (collected during
# checkout) is granted individual "reader" access to that exact file. A
# stolen/forwarded link is useless to anyone who isn't signed into Google as
# that specific email.
#
# Setup (one-time, per deployment):
#   1. Create a Google Cloud service account and enable the Drive API.
#   2. Download its JSON key and set GOOGLE_SERVICE_ACCOUNT_JSON (paste the
#      full JSON content) or GOOGLE_SERVICE_ACCOUNT_FILE (path to the key).
#   3. For every product file in Drive, share it with the service account's
#      email (shown in /admin -> Add Product) with "Editor" access, so the
#      bot is allowed to manage that file's sharing permissions.

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _load_drive_credentials():
    # Option 1: service account key.
    try:
        if GOOGLE_SERVICE_ACCOUNT_JSON:
            info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
            return service_account.Credentials.from_service_account_info(info, scopes=DRIVE_SCOPES)
        if GOOGLE_SERVICE_ACCOUNT_FILE:
            return service_account.Credentials.from_service_account_file(
                GOOGLE_SERVICE_ACCOUNT_FILE, scopes=DRIVE_SCOPES
            )
    except Exception:
        logger.exception("Failed to load Google service-account credentials.")

    # Option 2: OAuth refresh token for a regular Google account (this is what
    # you actually want if you want the file to live in a normal account's
    # Drive storage rather than a service account's, which has none by default).
    try:
        if GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and GOOGLE_REFRESH_TOKEN:
            return GoogleUserCredentials(
                token=None,
                refresh_token=GOOGLE_REFRESH_TOKEN,
                token_uri=GOOGLE_TOKEN_URI,
                client_id=GOOGLE_CLIENT_ID,
                client_secret=GOOGLE_CLIENT_SECRET,
                scopes=DRIVE_SCOPES,
            )
    except Exception:
        logger.exception("Failed to load Google OAuth refresh-token credentials.")

    return None


_drive_credentials = _load_drive_credentials()
_drive_service = None
_drive_connected_email = None  # lazily fetched + cached

if _drive_credentials is None:
    logger.warning(
        "Google Drive integration is NOT configured (no service-account credentials and no "
        "GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET/GOOGLE_REFRESH_TOKEN set). Orders can still be "
        "approved, but buyers will NOT automatically receive restricted access to their file "
        "until this is set up."
    )


def get_drive_connected_email():
    """Best-effort, cached lookup of which Google account is connected, so we
    can tell the admin who/what to share Drive files with. Returns None if
    Drive integration isn't configured or the lookup fails."""
    global _drive_connected_email
    if _drive_connected_email is not None:
        return _drive_connected_email

    sa_email = getattr(_drive_credentials, "service_account_email", None)
    if sa_email:
        _drive_connected_email = sa_email
        return sa_email

    service = get_drive_service()
    if service is None:
        return None
    try:
        about = service.about().get(fields="user(emailAddress)").execute()
        _drive_connected_email = about.get("user", {}).get("emailAddress")
    except Exception:
        logger.exception("Failed to fetch the connected Google Drive account's email.")
    return _drive_connected_email


def get_drive_service():
    global _drive_service
    if _drive_credentials is None:
        return None
    if _drive_service is None:
        _drive_service = build_drive_service("drive", "v3", credentials=_drive_credentials, cache_discovery=False)
    return _drive_service


def extract_drive_file_id(text: str):
    """Accepts a raw Drive File ID or a full share-link and returns the File ID,
    or None if nothing that looks like a valid ID could be found."""
    text = text.strip()
    for pattern in (r"/file/d/([-\w]{10,})", r"/d/([-\w]{10,})", r"[?&]id=([-\w]{10,})"):
        m = re.search(pattern, text)
        if m:
            return m.group(1)
    if re.fullmatch(r"[-\w]{10,}", text):
        return text
    return None


def is_valid_email(text: str) -> bool:
    return bool(EMAIL_RE.match(text.strip()))


def grant_drive_access(file_id: str, email: str):
    """Grants 'reader' access on the given Drive file to a specific email.
    Returns (success: bool, error_message: str | None)."""
    service = get_drive_service()
    if service is None:
        return False, "Google Drive integration is not configured on the server."
    try:
        service.permissions().create(
            fileId=file_id,
            body={"type": "user", "role": "reader", "emailAddress": email},
            sendNotificationEmail=False,
            fields="id",
        ).execute()
        return True, None
    except HttpError as e:
        logger.exception("Drive API error granting access to %s for file %s", email, file_id)
        return False, f"Drive API error: {e}"
    except Exception as e:
        logger.exception("Unexpected error granting Drive access to %s for file %s", email, file_id)
        return False, str(e)


# --------------------------------------------------------------------------- #
# Conversation states
# --------------------------------------------------------------------------- #

# Add product conversation
ADD_CATEGORY, ADD_TITLE, ADD_PRICE, ADD_DESCRIPTION, ADD_PHOTO, ADD_DRIVE_LINK = range(6)

# Edit price conversation
EP_CHOOSE, EP_ENTER = range(2)

# Edit payment conversation
PAY_CHOOSE, PAY_ENTER = range(2)

# Buy / checkout conversation
BUY_EMAIL, BUY_SCREENSHOT = range(2)

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def is_admin(user_id) -> bool:
    return int(user_id) == ADMIN_ID


def esc(text) -> str:
    return html.escape(str(text)) if text is not None else ""


def format_price(amount) -> str:
    return f"{amount:,.2f} {CURRENCY}"


ORDER_STATUS_EMOJI = {"pending": "⏳ Pending", "approved": "✅ Approved", "rejected": "❌ Rejected"}


def categories_keyboard():
    buttons = [
        [InlineKeyboardButton(f"{CATEGORY_EMOJI[c]} {c}", callback_data=f"menu_cat_{c}")]
        for c in CATEGORIES
    ]
    return InlineKeyboardMarkup(buttons)


def admin_menu_keyboard():
    buttons = [
        [InlineKeyboardButton("➕ Add Product", callback_data="admin_add")],
        [InlineKeyboardButton("❌ Remove Product", callback_data="admin_remove")],
        [InlineKeyboardButton("💰 Edit Price", callback_data="admin_editprice")],
        [InlineKeyboardButton("💳 Edit Payment Info", callback_data="admin_editpay")],
        [InlineKeyboardButton("📋 List Products", callback_data="admin_list")],
    ]
    return InlineKeyboardMarkup(buttons)


async def safe_edit_or_send(query, text, reply_markup=None, parse_mode=ParseMode.HTML):
    """Edit the message if it's a text message, otherwise send a new one."""
    try:
        if query.message and query.message.text is not None:
            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
            return
    except Exception:
        pass
    await query.message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)


async def conversation_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shared TIMEOUT handler for every ConversationHandler below. Prevents a
    user from ever getting permanently stuck mid-flow if they walk away."""
    context.user_data.clear()
    chat_id = update.effective_chat.id if update.effective_chat else None
    if chat_id is not None:
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text="⏳ This action timed out and was cancelled. Send /start or /admin to begin again.",
            )
        except Exception:
            logger.exception("Failed to notify chat %s about conversation timeout.", chat_id)
    return ConversationHandler.END


# --------------------------------------------------------------------------- #
# Public / Browsing handlers
# --------------------------------------------------------------------------- #


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        f"👋 <b>Welcome to {esc(STORE_NAME)}!</b>\n\n"
        "Browse our catalog of Books, Movies, and Music below. "
        "Tap a category to see what's available.\n\n"
        "ℹ️ Use /help any time to see how ordering works, or /myorders to check "
        "the status of a purchase."
    )
    await update.message.reply_text(text, reply_markup=categories_keyboard(), parse_mode=ParseMode.HTML)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        f"🛍️ <b>How {esc(STORE_NAME)} works</b>\n\n"
        "1️⃣ Use /start to browse categories.\n"
        "2️⃣ Tap a product to see its details.\n"
        "3️⃣ Tap <b>Buy Now</b>, follow the payment instructions, then reply with a "
        "screenshot of your payment.\n"
        "4️⃣ Once the payment is verified, you'll automatically receive your download "
        "link right here in the chat.\n\n"
        "📦 Use /myorders to check the status of your past orders.\n"
        "🚫 Use /cancel at any time to abort whatever you're currently doing."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def my_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    orders = get_orders_by_user(user.id)
    if not orders:
        await update.message.reply_text(
            "📭 You don't have any orders yet. Use /start to browse the catalog!"
        )
        return
    lines = ["📦 <b>Your Orders</b>\n"]
    for o in orders:
        title = esc(o["product_title"]) if o["product_title"] else "(product removed)"
        status_label = ORDER_STATUS_EMOJI.get(o["status"], esc(o["status"]))
        price = format_price(o["product_price"]) if o["product_price"] is not None else "—"
        lines.append(f"#{o['id']} <b>{title}</b> — {price}\nStatus: {status_label}\n")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def browse_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    category = query.data.split("menu_cat_", 1)[1]
    products = get_products_by_category(category)

    if not products:
        text = (
            f"{CATEGORY_EMOJI.get(category, '')} <b>{esc(category)}</b>\n\n"
            "😔 No products available yet. Check back soon!"
        )
        buttons = [[InlineKeyboardButton("⬅️ Back", callback_data="back_categories")]]
        await safe_edit_or_send(query, text, InlineKeyboardMarkup(buttons))
        return

    buttons = [
        [InlineKeyboardButton(f"{p['title']} — {format_price(p['price'])}", callback_data=f"view_prod_{p['id']}")]
        for p in products
    ]
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="back_categories")])
    text = f"{CATEGORY_EMOJI.get(category, '')} <b>{esc(category)}</b>\n\nSelect an item to view details:"
    await safe_edit_or_send(query, text, InlineKeyboardMarkup(buttons))


async def back_categories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    text = f"🛍️ <b>{esc(STORE_NAME)}</b>\n\nChoose a category:"
    await safe_edit_or_send(query, text, categories_keyboard())


async def view_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    product_id = int(query.data.split("view_prod_", 1)[1])
    product = get_product(product_id)
    if not product:
        await query.message.reply_text("😔 Sorry, this product is no longer available.")
        return

    caption = (
        f"<b>{esc(product['title'])}</b>\n"
        f"{CATEGORY_EMOJI.get(product['category'], '')} {esc(product['category'])}\n\n"
        f"{esc(product['description'])}\n\n"
        f"💵 <b>Price:</b> {format_price(product['price'])}"
    )
    buttons = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🛒 Buy Now", callback_data=f"buy_{product['id']}")],
            [InlineKeyboardButton("⬅️ Back", callback_data=f"menu_cat_{product['category']}")],
        ]
    )
    if product["photo_file_id"]:
        await query.message.reply_photo(
            product["photo_file_id"], caption=caption, reply_markup=buttons, parse_mode=ParseMode.HTML
        )
    else:
        await query.message.reply_text(caption, reply_markup=buttons, parse_mode=ParseMode.HTML)


# --------------------------------------------------------------------------- #
# Buy / Checkout conversation
# --------------------------------------------------------------------------- #


async def buy_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    product_id = int(query.data.split("buy_", 1)[1])
    product = get_product(product_id)
    if not product:
        await query.message.reply_text("😔 Sorry, this product is no longer available.")
        return ConversationHandler.END

    context.user_data.clear()
    context.user_data["buy_product_id"] = product_id

    text = (
        f"🛒 You're purchasing: <b>{esc(product['title'])}</b> — {format_price(product['price'])}\n\n"
        "📧 Since your file access will be personally restricted to you (not a public link "
        "anyone could share), please send the <b>Google/Gmail email address</b> you want "
        "access granted to."
    )
    buttons = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel Purchase", callback_data="buy_cancel")]])
    await query.message.reply_text(text, reply_markup=buttons, parse_mode=ParseMode.HTML)
    return BUY_EMAIL


async def buy_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await safe_edit_or_send(query, "🛑 Purchase cancelled. Use /start to keep browsing.")
    return ConversationHandler.END


async def buy_receive_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    email = update.message.text.strip()
    if not is_valid_email(email):
        await update.message.reply_text(
            "That doesn't look like a valid email address. Please send it again "
            "(e.g. yourname@gmail.com), or tap ❌ Cancel Purchase / send /cancel."
        )
        return BUY_EMAIL

    product_id = context.user_data.get("buy_product_id")
    product = get_product(product_id) if product_id else None
    if not product:
        await update.message.reply_text("Something went wrong, please start over with /start.")
        context.user_data.clear()
        return ConversationHandler.END

    context.user_data["buy_email"] = email
    settings = get_payment_settings()

    text = (
        f"✅ Access will be granted to: <b>{esc(email)}</b>\n\n"
        "💳 <b>Payment options</b>\n\n"
        f"📱 <b>Telebirr:</b>\n{esc(settings['telebirr'])}\n\n"
        f"🏦 <b>CBE:</b>\n{esc(settings['cbe'])}\n\n"
        "After sending the payment, reply here with a <b>screenshot</b> of your "
        "transaction so we can verify it."
    )
    buttons = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel Purchase", callback_data="buy_cancel")]])
    await update.message.reply_text(text, reply_markup=buttons, parse_mode=ParseMode.HTML)
    return BUY_SCREENSHOT


async def buy_invalid_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Please send a valid email address (e.g. yourname@gmail.com), "
        "or tap ❌ Cancel Purchase above / send /cancel."
    )
    return BUY_EMAIL


async def buy_receive_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    product_id = context.user_data.get("buy_product_id")
    buyer_email = context.user_data.get("buy_email")
    product = get_product(product_id) if product_id else None
    if not product or not buyer_email:
        await update.message.reply_text("Something went wrong, please start over with /start.")
        context.user_data.clear()
        return ConversationHandler.END

    photo_file_id = update.message.photo[-1].file_id
    user = update.effective_user
    order_id = create_order(user.id, user.username or user.full_name, product_id, buyer_email, photo_file_id)

    await update.message.reply_text(
        "✅ Your payment screenshot has been submitted for verification.\n"
        f"Order #{order_id} — access will be granted to <b>{esc(buyer_email)}</b> as soon as it's approved.\n\n"
        "Track it any time with /myorders.",
        parse_mode=ParseMode.HTML,
    )

    admin_caption = (
        f"🆕 <b>New Order #{order_id}</b>\n\n"
        f"👤 Buyer: {esc(user.full_name)} (@{esc(user.username) if user.username else 'no_username'}, id: {user.id})\n"
        f"📧 Grant access to: <b>{esc(buyer_email)}</b>\n"
        f"🛍️ Product: <b>{esc(product['title'])}</b> ({esc(product['category'])})\n"
        f"💵 Price: {format_price(product['price'])}\n\n"
        "Please verify the payment screenshot below."
    )
    buttons = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"approve_{order_id}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"reject_{order_id}"),
            ]
        ]
    )
    try:
        await context.bot.send_photo(
            chat_id=ADMIN_ID,
            photo=photo_file_id,
            caption=admin_caption,
            reply_markup=buttons,
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        logger.exception("Failed to notify admin about new order %s", order_id)

    context.user_data.clear()
    return ConversationHandler.END


async def buy_invalid_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Please send a screenshot image of your payment, or tap ❌ Cancel Purchase above / send /cancel."
    )
    return BUY_SCREENSHOT


async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("🚫 Operation cancelled.")
    return ConversationHandler.END


async def conversation_fallback_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Universal safety-net fallback added to every ConversationHandler below.

    Without this, a callback/message that doesn't match the handlers for the
    CURRENT conversation state is simply dropped: no handler runs, so
    query.answer() never gets called, and the tapped button spins forever in
    Telegram's UI. Worse, the user is left "stuck" inside that conversation,
    so even the original entry-point button (e.g. "Buy Now") stops working
    the next time they tap it, since the entry point is only checked when
    there is NO active conversation for that user yet.

    This handler guarantees two things:
      1. Every stray update always gets a real response (no infinite spinner).
      2. The stuck conversation is always force-ended, so the next tap of the
         entry-point button (which now also has allow_reentry=True) works
         immediately instead of staying broken indefinitely.
    """
    context.user_data.clear()
    query = update.callback_query
    if query is not None:
        try:
            await query.answer("Session expired — please tap the button again.", show_alert=True)
        except Exception:
            pass
        try:
            await safe_edit_or_send(
                query,
                "🔄 That session expired or is out of date. Please tap the button again, "
                "or send /start (or /admin) to begin fresh.",
            )
        except Exception:
            pass
    elif update.message is not None:
        try:
            await update.message.reply_text(
                "🔄 That didn't match what I was expecting, so the previous session was reset. "
                "Please send /start or /admin to begin again."
            )
        except Exception:
            pass
    return ConversationHandler.END


# --------------------------------------------------------------------------- #
# Order approval / rejection (Admin)
# --------------------------------------------------------------------------- #


async def approve_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("You're not authorized.", show_alert=True)
        return
    order_id = int(query.data.split("approve_", 1)[1])
    order = get_order(order_id)
    if not order:
        await query.answer("Order not found.", show_alert=True)
        return
    if order["status"] != "pending":
        await query.answer(f"Order already {order['status']}.", show_alert=True)
        return

    product = get_product(order["product_id"])
    update_order_status(order_id, "approved")
    buyer_email = order["buyer_email"]

    if product and buyer_email and product["drive_file_id"]:
        success, error = grant_drive_access(product["drive_file_id"], buyer_email)
        if success:
            try:
                await context.bot.send_message(
                    chat_id=order["user_id"],
                    text=(
                        "🎉 <b>Payment verified!</b>\n\n"
                        f"Access to <b>{esc(product['title'])}</b> has been granted to "
                        f"<b>{esc(buyer_email)}</b>.\n\n"
                        f"Open it here (make sure you're signed into Google as {esc(buyer_email)}):\n"
                        f"https://drive.google.com/file/d/{esc(product['drive_file_id'])}/view\n\n"
                        f"Thank you for shopping with {esc(STORE_NAME)}!"
                    ),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                logger.exception("Failed to notify buyer about order %s", order_id)
            await query.answer("Order approved — Drive access granted.")
        else:
            # Payment is verified either way, but the buyer can't access the
            # file yet. Tell the admin exactly what to fix instead of the
            # order silently succeeding with no actual delivery.
            logger.error("Drive access grant failed for order %s: %s", order_id, error)
            try:
                await context.bot.send_message(
                    chat_id=order["user_id"],
                    text=(
                        "🎉 <b>Payment verified!</b>\n\n"
                        f"We're finalizing access to <b>{esc(product['title'])}</b> for "
                        f"<b>{esc(buyer_email)}</b> — you'll get a follow-up message shortly."
                    ),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                logger.exception("Failed to notify buyer about order %s", order_id)
            await query.answer("Order approved, but Drive access FAILED. Check the admin chat for details.", show_alert=True)
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=(
                        f"⚠️ Order #{order_id} approved, but granting Drive access failed.\n"
                        f"Product: {esc(product['title'])} (file id: <code>{esc(product['drive_file_id'])}</code>)\n"
                        f"Buyer email: {esc(buyer_email)}\n"
                        f"Error: <code>{esc(error)}</code>\n\n"
                        "Make sure the file is shared with the service account as Editor, "
                        "then share it with the buyer manually if needed."
                    ),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                logger.exception("Failed to alert admin about Drive grant failure for order %s", order_id)
    elif product and not product["drive_file_id"]:
        await query.answer("Order approved, but this product has no Drive File ID set!", show_alert=True)
        logger.error("Order %s approved but product %s has no drive_file_id.", order_id, product["id"])
    else:
        await query.answer("Order approved, but buyer email or product is missing.", show_alert=True)

    new_caption = (query.message.caption or "") + "\n\n✅ <b>APPROVED</b>"
    try:
        await query.edit_message_caption(new_caption, parse_mode=ParseMode.HTML)
    except Exception:
        pass


async def reject_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await query.answer("You're not authorized.", show_alert=True)
        return
    order_id = int(query.data.split("reject_", 1)[1])
    order = get_order(order_id)
    if not order:
        await query.answer("Order not found.", show_alert=True)
        return
    if order["status"] != "pending":
        await query.answer(f"Order already {order['status']}.", show_alert=True)
        return

    update_order_status(order_id, "rejected")

    try:
        await context.bot.send_message(
            chat_id=order["user_id"],
            text=(
                "❌ We couldn't verify your payment for your recent order. "
                "Please double-check your transaction and try again, or contact support."
            ),
        )
    except Exception:
        logger.exception("Failed to notify user about rejected order %s", order_id)

    await query.answer("Order rejected.")
    new_caption = (query.message.caption or "") + "\n\n❌ <b>REJECTED</b>"
    try:
        await query.edit_message_caption(new_caption, parse_mode=ParseMode.HTML)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Admin panel
# --------------------------------------------------------------------------- #


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ You are not authorized to use this command.")
        return
    await update.message.reply_text(
        "🛠️ <b>Admin Panel</b>\n\nWhat would you like to do?",
        reply_markup=admin_menu_keyboard(),
        parse_mode=ParseMode.HTML,
    )


async def admin_list_products(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    products = get_all_products()
    if not products:
        await safe_edit_or_send(query, "No products in the catalog yet.", admin_menu_keyboard())
        return
    lines = ["📋 <b>All Products</b>\n"]
    for p in products:
        lines.append(
            f"#{p['id']} [{esc(p['category'])}] <b>{esc(p['title'])}</b> — {format_price(p['price'])}"
        )
    text = "\n".join(lines)
    buttons = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="admin_back")]])
    await safe_edit_or_send(query, text, buttons)


async def admin_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await safe_edit_or_send(query, "🛠️ <b>Admin Panel</b>\n\nWhat would you like to do?", admin_menu_keyboard())


# --- Add Product conversation --- #


async def admin_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return ConversationHandler.END
    context.user_data["new_product"] = {}
    buttons = InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"{CATEGORY_EMOJI[c]} {c}", callback_data=f"addcat_{c}")] for c in CATEGORIES]
    )
    await safe_edit_or_send(query, "➕ <b>Add Product</b>\n\nChoose a category:", buttons)
    return ADD_CATEGORY


async def admin_add_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    category = query.data.split("addcat_", 1)[1]
    context.user_data["new_product"]["category"] = category
    await query.message.reply_text("Great. Now send me the <b>title</b> of the product.", parse_mode=ParseMode.HTML)
    return ADD_TITLE


async def admin_add_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_product"]["title"] = update.message.text.strip()
    await update.message.reply_text(
        f"Now send the <b>price</b> in {esc(CURRENCY)} (numbers only, e.g. 150 or 99.50).",
        parse_mode=ParseMode.HTML,
    )
    return ADD_PRICE


async def admin_add_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        price = float(update.message.text.strip())
        if price < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("That doesn't look like a valid price. Please send a number, e.g. 150")
        return ADD_PRICE
    context.user_data["new_product"]["price"] = price
    await update.message.reply_text("Now send a short <b>description</b> of the product.", parse_mode=ParseMode.HTML)
    return ADD_DESCRIPTION


async def admin_add_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_product"]["description"] = update.message.text.strip()
    await update.message.reply_text(
        "Now send a <b>cover photo/image</b> for this product, or send /skip to add it without a cover photo.",
        parse_mode=ParseMode.HTML,
    )
    return ADD_PHOTO


async def admin_add_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("Please send an actual photo, or /skip to continue without one.")
        return ADD_PHOTO
    context.user_data["new_product"]["photo_file_id"] = update.message.photo[-1].file_id
    return await _prompt_for_drive_file_id(update, context)


async def admin_add_skip_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_product"]["photo_file_id"] = None
    return await _prompt_for_drive_file_id(update, context)


async def _prompt_for_drive_file_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    connected_email = get_drive_connected_email()
    if connected_email:
        setup_note = (
            f"⚠️ Make sure this file is owned by, or shared as <b>Editor</b> with, "
            f"<code>{esc(connected_email)}</code> — otherwise the bot can't grant buyers access to it."
        )
    else:
        setup_note = (
            "⚠️ Google Drive integration is not configured (or the connection couldn't be "
            "verified) on the server yet, so approvals won't be able to grant access "
            "automatically until that's set up."
        )
    await update.message.reply_text(
        "Finally, send the <b>Google Drive File ID</b> for this product's file "
        "(you can paste the full share link too, e.g. "
        "https://drive.google.com/file/d/FILE_ID/view — I'll extract the ID).\n\n"
        f"{setup_note}",
        parse_mode=ParseMode.HTML,
    )
    return ADD_DRIVE_LINK


async def admin_add_drive_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file_id = extract_drive_file_id(update.message.text)
    if not file_id:
        await update.message.reply_text(
            "I couldn't find a valid Drive File ID in that. Please paste the file's "
            "share link or its raw File ID."
        )
        return ADD_DRIVE_LINK

    data = context.user_data.get("new_product", {})
    product_id = add_product(
        data.get("category"),
        data.get("title"),
        data.get("price"),
        data.get("description"),
        data.get("photo_file_id"),
        file_id,
    )
    context.user_data.pop("new_product", None)
    await update.message.reply_text(
        f"✅ Product #{product_id} '<b>{esc(data.get('title'))}</b>' added successfully!\n"
        f"Drive File ID on record: <code>{esc(file_id)}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_menu_keyboard(),
    )
    return ConversationHandler.END


# --- Remove Product --- #


async def admin_remove_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    products = get_all_products()
    if not products:
        await safe_edit_or_send(query, "No products to remove.", admin_menu_keyboard())
        return
    buttons = [
        [InlineKeyboardButton(f"🗑️ #{p['id']} {p['title']}", callback_data=f"rm_{p['id']}")]
        for p in products
    ]
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="admin_back")])
    await safe_edit_or_send(query, "❌ <b>Remove Product</b>\n\nSelect a product to delete:", InlineKeyboardMarkup(buttons))


async def admin_remove_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    product_id = int(query.data.split("rm_", 1)[1])
    product = get_product(product_id)
    if product:
        delete_product(product_id)
        await safe_edit_or_send(query, f"🗑️ Product '<b>{esc(product['title'])}</b>' deleted.", admin_menu_keyboard())
    else:
        await safe_edit_or_send(query, "Product not found.", admin_menu_keyboard())


# --- Edit Price conversation --- #


async def admin_editprice_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return ConversationHandler.END
    products = get_all_products()
    if not products:
        await safe_edit_or_send(query, "No products available to edit.", admin_menu_keyboard())
        return ConversationHandler.END
    buttons = [
        [InlineKeyboardButton(f"#{p['id']} {p['title']} — {format_price(p['price'])}", callback_data=f"epsel_{p['id']}")]
        for p in products
    ]
    await safe_edit_or_send(query, "💰 <b>Edit Price</b>\n\nSelect a product:", InlineKeyboardMarkup(buttons))
    return EP_CHOOSE


async def admin_editprice_choose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    product_id = int(query.data.split("epsel_", 1)[1])
    product = get_product(product_id)
    if not product:
        await query.message.reply_text("Product not found.")
        return ConversationHandler.END
    context.user_data["editprice_id"] = product_id
    await query.message.reply_text(
        f"Current price of '<b>{esc(product['title'])}</b>' is {format_price(product['price'])}.\n"
        "Send the new price (numbers only).",
        parse_mode=ParseMode.HTML,
    )
    return EP_ENTER


async def admin_editprice_enter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        price = float(update.message.text.strip())
        if price < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Invalid price. Please send a number, e.g. 150")
        return EP_ENTER
    product_id = context.user_data.pop("editprice_id", None)
    if product_id is None:
        await update.message.reply_text("Something went wrong. Please try again from /admin.")
        return ConversationHandler.END
    update_product_price(product_id, price)
    await update.message.reply_text(f"✅ Price updated to {format_price(price)}.", reply_markup=admin_menu_keyboard())
    return ConversationHandler.END


# --- Edit Payment Info conversation --- #


async def admin_editpay_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return ConversationHandler.END
    buttons = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📱 Telebirr", callback_data="paych_telebirr")],
            [InlineKeyboardButton("🏦 CBE", callback_data="paych_cbe")],
        ]
    )
    await safe_edit_or_send(query, "💳 <b>Edit Payment Info</b>\n\nWhich method do you want to update?", buttons)
    return PAY_CHOOSE


async def admin_editpay_choose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    method = query.data.split("paych_", 1)[1]
    context.user_data["editpay_method"] = method
    await query.message.reply_text(
        f"Send the new payment details for <b>{method.upper()}</b> "
        "(e.g. account name and number, or instructions):",
        parse_mode=ParseMode.HTML,
    )
    return PAY_ENTER


async def admin_editpay_enter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    method = context.user_data.pop("editpay_method", None)
    if not method:
        await update.message.reply_text("Something went wrong. Please try again from /admin.")
        return ConversationHandler.END
    update_payment_setting(method, update.message.text.strip())
    await update.message.reply_text(f"✅ {method.upper()} payment info updated.", reply_markup=admin_menu_keyboard())
    return ConversationHandler.END


# --------------------------------------------------------------------------- #
# Error handler
# --------------------------------------------------------------------------- #


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception while handling an update:", exc_info=context.error)
    # Notify the admin in Telegram as well, so failures are visible immediately
    # instead of only showing up in server logs (a silent crash otherwise looks
    # exactly like "the button did nothing").
    try:
        error_text = f"{type(context.error).__name__}: {context.error}"
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"⚠️ Bot error:\n<code>{esc(error_text)}</code>",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        logger.exception("Failed to notify admin about the error above.")


# --------------------------------------------------------------------------- #
# aiohttp health-check web server (required for Render web services)
# --------------------------------------------------------------------------- #


async def health(request):
    return web.Response(text="OK")


async def start_web_server(application: Application):
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info("Health-check web server listening on port %s", PORT)


# --------------------------------------------------------------------------- #
# Bot commands menu (the square grid button next to the message bar)
# --------------------------------------------------------------------------- #


async def setup_commands(application: Application):
    """Registers the bot's commands so Telegram shows them in the square
    'commands' menu button next to the message input bar."""
    await application.bot.set_my_commands(
        [
            BotCommand("start", "🛍️ Browse the store"),
            BotCommand("myorders", "📦 Check your order status"),
            BotCommand("help", "❓ How this store works"),
            BotCommand("admin", "🛠️ Admin panel"),
            BotCommand("cancel", "🚫 Cancel current action"),
        ]
    )
    await application.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    logger.info("Bot commands menu configured.")


async def post_init(application: Application):
    """Runs once on startup: brings up the health-check web server and
    registers the commands menu button."""
    await start_web_server(application)
    await setup_commands(application)


# --------------------------------------------------------------------------- #
# Application setup
# --------------------------------------------------------------------------- #


def build_application() -> Application:
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    timeout_state = {ConversationHandler.TIMEOUT: [TypeHandler(Update, conversation_timeout)]}

    # --- Basic commands --- #
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("myorders", my_orders))
    application.add_handler(CommandHandler("admin", admin_panel))

    # --- Buy / Checkout conversation --- #
    # Registered before the other CallbackQueryHandlers so the "buy_<id>" pattern
    # is claimed by this ConversationHandler first.
    buy_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(buy_start, pattern=r"^buy_\d+$")],
        states={
            BUY_EMAIL: [
                CallbackQueryHandler(buy_cancel, pattern=r"^buy_cancel$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, buy_receive_email),
                MessageHandler(~filters.COMMAND, buy_invalid_email),
            ],
            BUY_SCREENSHOT: [
                CallbackQueryHandler(buy_cancel, pattern=r"^buy_cancel$"),
                MessageHandler(filters.PHOTO, buy_receive_screenshot),
                MessageHandler(~filters.COMMAND, buy_invalid_input),
            ],
            **timeout_state,
        },
        fallbacks=[
            CommandHandler("cancel", cancel_conversation),
            CallbackQueryHandler(conversation_fallback_reset),
            MessageHandler(filters.ALL, conversation_fallback_reset),
        ],
        conversation_timeout=CONVERSATION_TIMEOUT_SECONDS,
        name="buy_conv",
        persistent=False,
        allow_reentry=True,
    )
    application.add_handler(buy_conv)

    # --- Add Product conversation --- #
    add_product_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_add_start, pattern=r"^admin_add$")],
        states={
            ADD_CATEGORY: [CallbackQueryHandler(admin_add_category, pattern=r"^addcat_(Books|Movies|Music)$")],
            ADD_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_title)],
            ADD_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_price)],
            ADD_DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_description)],
            ADD_PHOTO: [
                CommandHandler("skip", admin_add_skip_photo),
                MessageHandler(filters.PHOTO, admin_add_photo),
            ],
            ADD_DRIVE_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_drive_link)],
            **timeout_state,
        },
        fallbacks=[
            CommandHandler("cancel", cancel_conversation),
            CallbackQueryHandler(conversation_fallback_reset),
            MessageHandler(filters.ALL, conversation_fallback_reset),
        ],
        conversation_timeout=CONVERSATION_TIMEOUT_SECONDS,
        name="add_product_conv",
        persistent=False,
        allow_reentry=True,
    )
    application.add_handler(add_product_conv)

    # --- Edit Price conversation --- #
    edit_price_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_editprice_start, pattern=r"^admin_editprice$")],
        states={
            EP_CHOOSE: [CallbackQueryHandler(admin_editprice_choose, pattern=r"^epsel_\d+$")],
            EP_ENTER: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_editprice_enter)],
            **timeout_state,
        },
        fallbacks=[
            CommandHandler("cancel", cancel_conversation),
            CallbackQueryHandler(conversation_fallback_reset),
            MessageHandler(filters.ALL, conversation_fallback_reset),
        ],
        conversation_timeout=CONVERSATION_TIMEOUT_SECONDS,
        name="edit_price_conv",
        persistent=False,
        allow_reentry=True,
    )
    application.add_handler(edit_price_conv)

    # --- Edit Payment conversation --- #
    edit_pay_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_editpay_start, pattern=r"^admin_editpay$")],
        states={
            PAY_CHOOSE: [CallbackQueryHandler(admin_editpay_choose, pattern=r"^paych_(telebirr|cbe)$")],
            PAY_ENTER: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_editpay_enter)],
            **timeout_state,
        },
        fallbacks=[
            CommandHandler("cancel", cancel_conversation),
            CallbackQueryHandler(conversation_fallback_reset),
            MessageHandler(filters.ALL, conversation_fallback_reset),
        ],
        conversation_timeout=CONVERSATION_TIMEOUT_SECONDS,
        name="edit_pay_conv",
        persistent=False,
        allow_reentry=True,
    )
    application.add_handler(edit_pay_conv)

    # --- Remaining simple callback handlers --- #
    application.add_handler(CallbackQueryHandler(browse_category, pattern=r"^menu_cat_"))
    application.add_handler(CallbackQueryHandler(back_categories, pattern=r"^back_categories$"))
    application.add_handler(CallbackQueryHandler(view_product, pattern=r"^view_prod_\d+$"))
    application.add_handler(CallbackQueryHandler(approve_order, pattern=r"^approve_\d+$"))
    application.add_handler(CallbackQueryHandler(reject_order, pattern=r"^reject_\d+$"))
    application.add_handler(CallbackQueryHandler(admin_list_products, pattern=r"^admin_list$"))
    application.add_handler(CallbackQueryHandler(admin_remove_list, pattern=r"^admin_remove$"))
    application.add_handler(CallbackQueryHandler(admin_remove_confirm, pattern=r"^rm_\d+$"))
    application.add_handler(CallbackQueryHandler(admin_back, pattern=r"^admin_back$"))

    application.add_error_handler(error_handler)

    return application


def main():
    logger.info("Starting %s bot...", STORE_NAME)
    init_db()
    application = build_application()

    if application.job_queue is None:
        logger.warning(
            "JobQueue is not available (the 'job-queue' extra is not installed). "
            "This means conversation_timeout will NOT fire, so if a user's session "
            "gets stuck it will never auto-recover on its own — only allow_reentry "
            "and the fallback safety-net will save it. Run "
            "'pip install \"python-telegram-bot[job-queue]\"' to enable full auto-recovery."
        )

    logger.info("Starting bot polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
