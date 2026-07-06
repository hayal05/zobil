"""
Digital Store Telegram Bot (Books / Movies / Music)
-----------------------------------------------------
Production-ready single-file bot built with python-telegram-bot v20+.

Features:
- Browse products by category (Books / Movies / Music) via inline keyboards.
- View product details (title, category, description, cover photo, price) with a "Buy Now" button.
- Manual payment flow (Telebirr / CBE) with screenshot upload for verification.
- Admin approval/rejection of orders. On approval, the Google Drive delivery link is
  automatically sent to the buyer. On rejection, the buyer is notified.
- Full admin panel (/admin) restricted to ADMIN_ID:
    - Add product (category, title, price, description, photo, Google Drive link)
    - Remove product
    - Edit price
    - Edit Telebirr / CBE payment details
    - List products
- SQLite persistence (products, payment_settings, orders) created automatically on startup.
- Built-in aiohttp web server bound to $PORT so Render's health check passes and the
  service does not get killed for "no open ports".

Environment variables required:
    TELEGRAM_BOT_TOKEN  - the bot token from @BotFather
    ADMIN_ID            - your numeric Telegram user id (string or int)

Optional:
    PORT                - defaults to 8080 (Render sets this automatically)
"""

import os
import html
import logging
import sqlite3
from datetime import datetime

from aiohttp import web

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
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
ADMIN_ID = os.environ.get("ADMIN_ID")
PORT = int(os.environ.get("PORT", 8080))
DB_FILE = os.environ.get("DB_FILE", "store.db")

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN environment variable is not set.")
if not ADMIN_ID:
    raise RuntimeError("ADMIN_ID environment variable is not set.")

CATEGORIES = ["Books", "Movies", "Music"]
CATEGORY_EMOJI = {"Books": "📚", "Movies": "🎬", "Music": "🎵"}

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
            drive_link TEXT NOT NULL
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
            screenshot_file_id TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT
        )
        """
    )
    cur.execute("INSERT OR IGNORE INTO payment_settings (id, telebirr, cbe) VALUES (1, ?, ?)",
                ("Not configured yet.", "Not configured yet."))
    conn.commit()
    conn.close()


def db_execute(query, params=(), fetchone=False, fetchall=False, commit=False):
    conn = get_conn()
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
    conn.close()
    return result


def add_product(category, title, price, description, photo_file_id, drive_link):
    return db_execute(
        """INSERT INTO products (category, title, price, description, photo_file_id, drive_link)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (category, title, price, description, photo_file_id, drive_link),
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


def create_order(user_id, username, product_id, screenshot_file_id):
    return db_execute(
        """INSERT INTO orders (user_id, username, product_id, screenshot_file_id, status, created_at)
           VALUES (?, ?, ?, ?, 'pending', ?)""",
        (user_id, username, product_id, screenshot_file_id, datetime.utcnow().isoformat()),
        commit=True,
    )


def get_order(order_id):
    return db_execute("SELECT * FROM orders WHERE id = ?", (order_id,), fetchone=True)


def update_order_status(order_id, status):
    db_execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id), commit=True)


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
BUY_SCREENSHOT = 0

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def is_admin(user_id) -> bool:
    return str(user_id) == str(ADMIN_ID)


def esc(text) -> str:
    return html.escape(str(text)) if text is not None else ""


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


# --------------------------------------------------------------------------- #
# Public / Browsing handlers
# --------------------------------------------------------------------------- #


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 <b>Welcome to the Digital Store!</b>\n\n"
        "Browse our catalog of Books, Movies, and Music. "
        "Pick a category to get started."
    )
    await update.message.reply_text(text, reply_markup=categories_keyboard(), parse_mode=ParseMode.HTML)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🛍️ <b>How this store works</b>\n\n"
        "1. Use /start to browse categories.\n"
        "2. Tap a product to see its details.\n"
        "3. Tap <b>Buy Now</b>, follow the payment instructions, then send a screenshot "
        "of your payment.\n"
        "4. Once the admin verifies your payment, you'll automatically receive your "
        "Google Drive download link right here in the chat.\n\n"
        "Use /cancel at any time to abort whatever you're currently doing."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def browse_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    category = query.data.split("menu_cat_", 1)[1]
    products = get_products_by_category(category)

    if not products:
        text = f"{CATEGORY_EMOJI.get(category, '')} <b>{esc(category)}</b>\n\nNo products available yet. Check back soon!"
        buttons = [[InlineKeyboardButton("⬅️ Back", callback_data="back_categories")]]
        await safe_edit_or_send(query, text, InlineKeyboardMarkup(buttons))
        return

    buttons = [
        [InlineKeyboardButton(f"{p['title']} — {p['price']:.2f} ETB", callback_data=f"view_prod_{p['id']}")]
        for p in products
    ]
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="back_categories")])
    text = f"{CATEGORY_EMOJI.get(category, '')} <b>{esc(category)}</b>\n\nSelect an item to view details:"
    await safe_edit_or_send(query, text, InlineKeyboardMarkup(buttons))


async def back_categories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    text = "🛍️ <b>Choose a category</b>"
    await safe_edit_or_send(query, text, categories_keyboard())


async def view_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    product_id = int(query.data.split("view_prod_", 1)[1])
    product = get_product(product_id)
    if not product:
        await query.message.reply_text("Sorry, this product is no longer available.")
        return

    caption = (
        f"<b>{esc(product['title'])}</b>\n"
        f"{CATEGORY_EMOJI.get(product['category'], '')} {esc(product['category'])}\n\n"
        f"{esc(product['description'])}\n\n"
        f"💵 <b>Price:</b> {product['price']:.2f} ETB"
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
        await query.message.reply_text("Sorry, this product is no longer available.")
        return ConversationHandler.END

    context.user_data["buy_product_id"] = product_id
    settings = get_payment_settings()

    text = (
        f"🛒 You're purchasing: <b>{esc(product['title'])}</b> — {product['price']:.2f} ETB\n\n"
        "💳 <b>Payment options</b>\n\n"
        f"📱 <b>Telebirr:</b>\n{esc(settings['telebirr'])}\n\n"
        f"🏦 <b>CBE:</b>\n{esc(settings['cbe'])}\n\n"
        "After sending the payment, please reply here with a <b>screenshot</b> of your "
        "transaction so we can verify it.\n\nSend /cancel to abort the purchase."
    )
    await query.message.reply_text(text, parse_mode=ParseMode.HTML)
    return BUY_SCREENSHOT


async def buy_receive_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    product_id = context.user_data.get("buy_product_id")
    product = get_product(product_id) if product_id else None
    if not product:
        await update.message.reply_text("Something went wrong, please start over with /start.")
        context.user_data.pop("buy_product_id", None)
        return ConversationHandler.END

    photo_file_id = update.message.photo[-1].file_id
    user = update.effective_user
    order_id = create_order(user.id, user.username or user.full_name, product_id, photo_file_id)

    await update.message.reply_text(
        "✅ Your payment screenshot has been submitted for verification. "
        "You'll receive your download link here as soon as it's approved."
    )

    admin_caption = (
        f"🆕 <b>New Order #{order_id}</b>\n\n"
        f"👤 Buyer: {esc(user.full_name)} (@{esc(user.username) if user.username else 'no_username'}, id: {user.id})\n"
        f"🛍️ Product: <b>{esc(product['title'])}</b> ({esc(product['category'])})\n"
        f"💵 Price: {product['price']:.2f} ETB\n\n"
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
            chat_id=int(ADMIN_ID),
            photo=photo_file_id,
            caption=admin_caption,
            reply_markup=buttons,
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        logger.exception("Failed to notify admin about new order %s", order_id)

    context.user_data.pop("buy_product_id", None)
    return ConversationHandler.END


async def buy_invalid_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Please send a screenshot image of your payment, or /cancel to abort.")
    return BUY_SCREENSHOT


async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Operation cancelled.")
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

    if product:
        try:
            await context.bot.send_message(
                chat_id=order["user_id"],
                text=(
                    "🎉 <b>Payment verified!</b>\n\n"
                    f"Here is your download link for <b>{esc(product['title'])}</b>:\n"
                    f"{esc(product['drive_link'])}\n\n"
                    "Thank you for your purchase!"
                ),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            logger.exception("Failed to send delivery link for order %s", order_id)

    await query.answer("Order approved and link delivered.")
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
            f"#{p['id']} [{esc(p['category'])}] <b>{esc(p['title'])}</b> — {p['price']:.2f} ETB"
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
    await update.message.reply_text("Now send the <b>price</b> in ETB (numbers only, e.g. 150 or 99.50).", parse_mode=ParseMode.HTML)
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
    await update.message.reply_text("Now send a <b>cover photo/image</b> for this product.", parse_mode=ParseMode.HTML)
    return ADD_PHOTO


async def admin_add_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("Please send an actual photo.")
        return ADD_PHOTO
    context.user_data["new_product"]["photo_file_id"] = update.message.photo[-1].file_id
    await update.message.reply_text(
        "Finally, send the <b>Google Drive delivery link</b> for this product "
        "(this will be sent to buyers automatically once their payment is approved).",
        parse_mode=ParseMode.HTML,
    )
    return ADD_DRIVE_LINK


async def admin_add_drive_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    link = update.message.text.strip()
    data = context.user_data.get("new_product", {})
    product_id = add_product(
        data.get("category"),
        data.get("title"),
        data.get("price"),
        data.get("description"),
        data.get("photo_file_id"),
        link,
    )
    context.user_data.pop("new_product", None)
    await update.message.reply_text(
        f"✅ Product #{product_id} '<b>{esc(data.get('title'))}</b>' added successfully!",
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
        [InlineKeyboardButton(f"#{p['id']} {p['title']} — {p['price']:.2f} ETB", callback_data=f"epsel_{p['id']}")]
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
        f"Current price of '<b>{esc(product['title'])}</b>' is {product['price']:.2f} ETB.\n"
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
    await update.message.reply_text(f"✅ Price updated to {price:.2f} ETB.", reply_markup=admin_menu_keyboard())
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
# Application setup
# --------------------------------------------------------------------------- #


def build_application() -> Application:
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(start_web_server).build()

    # --- Basic commands --- #
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("admin", admin_panel))

    # --- Add Product conversation --- #
    add_product_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_add_start, pattern=r"^admin_add$")],
        states={
            ADD_CATEGORY: [CallbackQueryHandler(admin_add_category, pattern=r"^addcat_(Books|Movies|Music)$")],
            ADD_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_title)],
            ADD_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_price)],
            ADD_DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_description)],
            ADD_PHOTO: [MessageHandler(filters.PHOTO, admin_add_photo)],
            ADD_DRIVE_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_drive_link)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
        name="add_product_conv",
        persistent=False,
    )
    application.add_handler(add_product_conv)

    # --- Edit Price conversation --- #
    edit_price_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_editprice_start, pattern=r"^admin_editprice$")],
        states={
            EP_CHOOSE: [CallbackQueryHandler(admin_editprice_choose, pattern=r"^epsel_\d+$")],
            EP_ENTER: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_editprice_enter)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
        name="edit_price_conv",
        persistent=False,
    )
    application.add_handler(edit_price_conv)

    # --- Edit Payment conversation --- #
    edit_pay_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_editpay_start, pattern=r"^admin_editpay$")],
        states={
            PAY_CHOOSE: [CallbackQueryHandler(admin_editpay_choose, pattern=r"^paych_(telebirr|cbe)$")],
            PAY_ENTER: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_editpay_enter)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
        name="edit_pay_conv",
        persistent=False,
    )
    application.add_handler(edit_pay_conv)

    # --- Buy / Checkout conversation --- #
    buy_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(buy_start, pattern=r"^buy_\d+$")],
        states={
            BUY_SCREENSHOT: [
                MessageHandler(filters.PHOTO, buy_receive_screenshot),
                MessageHandler(~filters.COMMAND, buy_invalid_input),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
        name="buy_conv",
        persistent=False,
    )
    application.add_handler(buy_conv)

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
    init_db()
    application = build_application()
    logger.info("Starting bot polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
