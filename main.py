import os
import json
import logging
import sqlite3
import html
import asyncio
import csv
from io import StringIO
from dotenv import load_dotenv
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

import httpx  # Required for fetching Google Sheet

# FastAPI Imports
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Aiogram Imports (Modern Async Telegram Bot)
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.types import WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton, MenuButtonWebApp, LabeledPrice, \
    PreCheckoutQuery

load_dotenv()

# --- CONFIGURATION ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
APP_URL = "https://cove-extended-about-unable.trycloudflare.com/?v=7"

# Channel Configuration for Automated Posting
CHANNEL_USERNAME = "@digitaldashmarkets"  # Put your exact public channel handle here

# --- EMAIL CONFIGURATION ---
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 465
SENDER_EMAIL = "digitaldashmarkets@gmail.com"
SENDER_PASSWORD = "ttaucdthpjutjtlv"
ADMIN_EMAIL = "digitaldashmarkets@gmail.com"

# Google Sheets Configuration
SHEET_ID = "1p8Jhnvx4aefvYyrF-iyMOov_irksBLD0Wz3Fa4NJ3iQ"
CSV_EXPORT_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/gviz/tq?tqx=out:csv&sheet=Sheet1"

if not BOT_TOKEN:
    raise ValueError("Missing BOT_TOKEN in .env file.")


# --- DATABASE SETUP (Users & Orders stored in Stars balance) ---
def init_db():
    conn = sqlite3.connect('database.db')
    cursor = conn.cursor()
    cursor.execute('''
                   CREATE TABLE IF NOT EXISTS users
                   (
                       user_id
                       INTEGER
                       PRIMARY
                       KEY,
                       username
                       TEXT,
                       first_name
                       TEXT,
                       points
                       INTEGER
                       DEFAULT
                       0,
                       tasks_completed
                       INTEGER
                       DEFAULT
                       0,
                       referred_by
                       INTEGER
                       DEFAULT
                       NULL,
                       wallet_address
                       TEXT
                       DEFAULT
                       NULL,
                       last_withdrawal_amount
                       INTEGER
                       DEFAULT
                       0
                   )
                   ''')
    cursor.execute('''
                   CREATE TABLE IF NOT EXISTS orders
                   (
                       order_id
                       INTEGER
                       PRIMARY
                       KEY
                       AUTOINCREMENT,
                       telegram_user_id
                       INTEGER,
                       username
                       TEXT,
                       email
                       TEXT,
                       items_json
                       TEXT,
                       total_cost
                       REAL,
                       timestamp
                       TEXT
                   )
                   ''')
    conn.commit()
    conn.close()


def save_order_to_db(order_data: dict):
    try:
        conn = sqlite3.connect('database.db')
        cursor = conn.cursor()
        cursor.execute('''
                       INSERT INTO orders (telegram_user_id, username, email, items_json, total_cost, timestamp)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ''', (
                           order_data['telegram_user_id'],
                           order_data['username'],
                           order_data['email'],
                           json.dumps(order_data['items']),
                           order_data['total_cost'],
                           datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                       ))
        conn.commit()
        conn.close()
        logger.info("💾 Order successfully saved to local database.db!")
        return True
    except Exception as e:
        logger.error(f"❌ Failed to save order to database: {e}")
        return False


def get_or_create_user(user_id, username, first_name, referrer_id=None):
    conn = sqlite3.connect('database.db')
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
    user = cursor.fetchone()

    if not user:
        ref = referrer_id if referrer_id and str(referrer_id) != str(user_id) else None
        # Giving 500 Stars (~$10.00) initially for testing
        cursor.execute('''
                       INSERT INTO users (user_id, username, first_name, points, tasks_completed, referred_by)
                       VALUES (?, ?, ?, 500, 0, ?)
                       ''', (user_id, username, first_name, ref))
        conn.commit()
        user = (user_id, username, first_name, 500, 0, ref, None, 0)
    conn.close()
    return user


def update_user_balance(user_id, stars_to_deduct):
    conn = sqlite3.connect('database.db')
    cursor = conn.cursor()
    cursor.execute('UPDATE users SET points = points - ? WHERE user_id = ?', (stars_to_deduct, user_id))
    conn.commit()
    conn.close()


def get_user_balance(user_id):
    conn = sqlite3.connect('database.db')
    cursor = conn.cursor()
    cursor.execute('SELECT points FROM users WHERE user_id = ?', (user_id,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else 0


def add_user_balance(user_id, stars_to_add):
    conn = sqlite3.connect('database.db')
    cursor = conn.cursor()
    cursor.execute('UPDATE users SET points = points + ? WHERE user_id = ?', (stars_to_add, user_id))
    conn.commit()
    conn.close()


init_db()


# --- GOOGLE SHEETS HELPER ---
async def fetch_products_from_sheets():
    async with httpx.AsyncClient(follow_redirects=True) as client:
        try:
            response = await client.get(CSV_EXPORT_URL)
            response.raise_for_status()
            csv_data = response.text
            if "<html" in csv_data.lower(): return []
        except Exception as e:
            return []

    products = []
    reader = csv.DictReader(StringIO(csv_data))
    for row in reader:
        try:
            cleaned_row = {k.strip().lower(): v.strip() for k, v in row.items() if k}
            product = {
                "id": int(cleaned_row["id"]),
                "name": cleaned_row["name"],
                "price": float(cleaned_row["price"]),
                "type": cleaned_row["type"],
                "description": cleaned_row["description"],
                "image": cleaned_row["image"]
            }
            products.append(product)
        except (ValueError, KeyError):
            continue
    return products


# --- FASTAPI BACKEND ---
app = FastAPI(title="DigitalDashMarkets API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


class OrderRequest(BaseModel):
    telegram_user_id: int
    username: str = None
    email: str
    items: list
    total_cost: float


class FundRequest(BaseModel):
    telegram_user_id: int
    amount: float


def send_order_email(order_data: dict):
    try:
        items_summary = "\n".join([
            f"- {item['name']} (Qty: {item['quantity']}) - ${item['price'] * item['quantity']:.2f}"
            for item in order_data['items']
        ])
        subject = f"🚨 New Order from {order_data['email']}"
        body = f"""
🛒 NEW MARKETPLACE ORDER RECEIVED!

👤 Telegram User ID: {order_data['telegram_user_id']}
🏷️ Username: @{order_data['username']}
📧 Customer Email: {order_data['email']}

🛍️ ITEMS PURCHASED:
{items_summary}

💰 TOTAL PAID: ${order_data['total_cost']:.2f}
        """
        msg = MIMEMultipart()
        msg['From'] = SENDER_EMAIL
        msg['To'] = ADMIN_EMAIL
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain'))

        server = smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT)
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
        server.send_message(msg)
        server.quit()
        logger.info("✅ Order email sent successfully!")
        return True
    except Exception as e:
        logger.warning(f"⚠️ Email dispatch skipped/failed: {e}")
        return False


@app.get("/", response_class=HTMLResponse)
async def serve_mini_app():
    products = await fetch_products_from_sheets()
    try:
        with open("index.html", "r", encoding="utf-8") as file:
            html_content = file.read()
        injected_script = f"<script>window.INITIAL_PRODUCTS = {json.dumps(products)};</script>"
        html_content = html_content.replace("</head>", f"{injected_script}</head>")
        return HTMLResponse(content=html_content, status_code=200)
    except FileNotFoundError:
        return HTMLResponse(content="<h1>Error: index.html not found</h1>", status_code=404)


@app.get("/api/products")
async def get_products():
    products = await fetch_products_from_sheets()
    return {"status": "success", "data": products}


@app.get("/api/balance/{user_id}")
async def fetch_balance(user_id: int):
    balance = get_user_balance(user_id)
    return {"status": "success", "balance": balance}


@app.get("/api/orders/{user_id}")
async def get_user_orders(user_id: int):
    conn = sqlite3.connect('database.db')
    cursor = conn.cursor()
    cursor.execute('''
                   SELECT order_id, items_json, total_cost, timestamp
                   FROM orders
                   WHERE telegram_user_id = ?
                   ORDER BY timestamp DESC
                   ''', (user_id,))
    rows = cursor.fetchall()
    conn.close()

    orders = []
    for row in rows:
        orders.append({
            "order_id": row[0],
            "items": json.loads(row[1]),
            "total_cost": row[2],
            "timestamp": row[3]
        })
    return {"status": "success", "orders": orders}


@app.post("/api/submit-order")
async def submit_order(order: OrderRequest):
    order_dict = order.dict()
    stars_required = int(order.total_cost / 0.02)

    current_balance_stars = get_user_balance(order.telegram_user_id)
    if current_balance_stars < stars_required:
        raise HTTPException(status_code=400, detail="Insufficient funds")

    update_user_balance(order.telegram_user_id, stars_required)
    save_order_to_db(order_dict)
    send_order_email(order_dict)

    new_balance = get_user_balance(order.telegram_user_id)
    return {"status": "success", "message": "Order processed successfully!", "new_balance": new_balance}


@app.post("/api/fund/stars")
async def generate_stars_invoice(req: FundRequest):
    stars_amount = int(req.amount / 0.02)
    prices = [LabeledPrice(label=f"Fund ${req.amount} Wallet", amount=stars_amount)]

    try:
        invoice_link = await bot.create_invoice_link(
            title=f"Fund ${req.amount} Balance",
            description="Top up your DigitalDashMarkets wallet with Telegram Stars.",
            payload=f"topup_{req.telegram_user_id}_{stars_amount}",
            provider_token="",
            currency="XTR",
            prices=prices
        )
        return {"status": "success", "url": invoice_link}
    except Exception as e:
        logger.error(f"Stars invoice error: {e}")
        raise HTTPException(status_code=400, detail=str(e))


# --- AUTOMATED BACKGROUND CHANNEL POSTING LOOP ---
async def channel_poster_loop():
    """Automatically cycles through and posts products from Google Sheets to your channel every 3 hours."""
    await asyncio.sleep(15)  # Initial startup delay

    product_index = 0
    while True:
        try:
            logger.info("🔄 Background task fetching products for channel feed...")
            products = await fetch_products_from_sheets()

            if products:
                # Cycle safely through products index
                if product_index >= len(products):
                    product_index = 0

                p = products[product_index]
                product_index += 1

                stars_price = int(p["price"] / 0.02)

                caption = (
                    f"🔥 <b>{html.escape(p['name'])}</b>\n\n"
                    f"📝 {html.escape(p['description'])}\n\n"
                    f"💵 <b>Price:</b> ${p['price']:.2f} USD\n"
                    f"⭐ <b>Stars:</b> {stars_price} Stars\n\n"
                    f"👇 Tap below to open store instantly!"
                )

                # DIRECT BOT APP LINK (Opens natively without browser link warnings)
                NATIVE_APP_URL = "https://t.me/TGMiniAppMarket_bot/DigitalGoodsDash"

                markup = InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="🛒 Open Store", url=NATIVE_APP_URL)]]
                )

                await bot.send_photo(
                    chat_id=CHANNEL_USERNAME,
                    photo=p["image"],
                    caption=caption,
                    reply_markup=markup,
                    parse_mode='HTML'
                )
                logger.info(f"✅ Successfully posted product '{p['name']}' to {CHANNEL_USERNAME}!")
            else:
                logger.warning("⚠️ No products found in Google Sheets to broadcast.")

        except Exception as e:
            logger.error(f"❌ Error in channel poster background task: {e}")

        # 3 Hours interval (10800 seconds).
        # (Keep it at 30 seconds temporarily if you are still testing rotation!)
        await asyncio.sleep(30)


# --- TELEGRAM BOT LOGIC ---
@dp.message(CommandStart())
async def command_start_handler(message: types.Message) -> None:
    user = message.from_user
    get_or_create_user(user.id, user.username, user.first_name)

    markup = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🚀 Tap Here to Open Store", web_app=WebAppInfo(url=APP_URL))]]
    )
    await message.answer("Welcome to DigitalDashMarkets. Tap below to launch your store:", reply_markup=markup)


@dp.pre_checkout_query()
async def pre_checkout_query_handler(pre_checkout_query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)


@dp.message(F.successful_payment)
async def successful_payment_handler(message: types.Message):
    payment_info = message.successful_payment
    payload = payment_info.invoice_payload
    if payload.startswith("topup_"):
        parts = payload.split("_")
        user_id = int(parts[1])
        stars_added = int(parts[2])
        add_user_balance(user_id, stars_added)
        await message.answer(f"✅ Successfully added ⭐ {stars_added} Stars to your wallet balance!")


@app.on_event("startup")
async def on_startup():
    logger.info("Setting up Telegram Bot...")
    await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Open Store", web_app=WebAppInfo(url=APP_URL)))

    # Start bot polling and channel posting loop in the background
    asyncio.create_task(dp.start_polling(bot))
    asyncio.create_task(channel_poster_loop())
    logger.info("🚀 Automated channel feed loop initialized!")


if __name__ == "__main__":
    import uvicorn

    print("Starting Server & Bot...")
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)