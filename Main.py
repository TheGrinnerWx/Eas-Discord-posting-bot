# Save this code in a file named EAS.py (or BOT_GlobalEAS.py)
import discord
from discord.ext import tasks, commands
import aiohttp # For asynchronous HTTP requests
import json
import os
import asyncio
from datetime import datetime, timezone
import logging
# No gTTS needed if using downloaded audio
# No BeautifulSoup needed as we're using the API
import io
from dotenv import load_dotenv
# Add urlparse import for filename extraction (used later)
from urllib.parse import urlparse


# --- Load Environment Variables ---
load_dotenv()

# --- Configuration ---
DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
ALERT_CHANNEL_ID_STR = os.getenv('ALERT_CHANNEL_ID')
LOG_CHANNEL_ID_STR = os.getenv('LOG_CHANNEL_ID') # Optional
CHECK_INTERVAL_SECONDS_STR = os.getenv('CHECK_INTERVAL_SECONDS', '120') # Default 120 seconds
# NWS_API_USER_AGENT is NOT needed for this API
# ALERT_AREA_CODES is NOT needed (API likely doesn't filter by NWS area code)
TARGET_API_URL = "https://alerts.globaleas.org/api/v1/alerts/active" # Use the confirmed API endpoint
PERSISTENCE_FILE = "posted_globaleas_alerts.txt" # File to store posted alert hashes

# --- Validate Configuration ---
if not DISCORD_BOT_TOKEN: print("CRITICAL: DISCORD_BOT_TOKEN missing."); exit()
if not ALERT_CHANNEL_ID_STR: print("CRITICAL: ALERT_CHANNEL_ID missing."); exit()
try: ALERT_CHANNEL_ID = int(ALERT_CHANNEL_ID_STR)
except ValueError: print(f"CRITICAL: ALERT_CHANNEL_ID invalid."); exit()
LOG_CHANNEL_ID = None
if LOG_CHANNEL_ID_STR:
    try: LOG_CHANNEL_ID = int(LOG_CHANNEL_ID_STR)
    except ValueError: print(f"WARNING: LOG_CHANNEL_ID invalid."); LOG_CHANNEL_ID = None
try:
    CHECK_INTERVAL_SECONDS = int(CHECK_INTERVAL_SECONDS_STR)
    if CHECK_INTERVAL_SECONDS < 30: CHECK_INTERVAL_SECONDS = 30
except ValueError:
    if os.getenv('CHECK_INTERVAL_SECONDS') is not None: print(f"WARNING: CHECK_INTERVAL_SECONDS invalid.");
    CHECK_INTERVAL_SECONDS = 120

# --- Relevant Events Set (Optional - Can be used later if needed) ---
# RELEVANT_EVENTS = {"TOR", "SVR", ...} # Example EAS Codes

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s:%(levelname)s:%(name)s: %(message)s')
logger = logging.getLogger('GlobalEASBot')

# --- Bot Setup ---
intents = discord.Intents.default()
# intents.message_content = True # Not needed unless using message content commands
bot = commands.Bot(command_prefix="!", intents=intents)
posted_alert_identifiers = set() # Initialize in-memory set

# --- Persistence Functions --- ## CORRECTED INDENTATION ##
def load_posted_ids(filename):
    """Loads posted alert IDs from a file into a set."""
    ids = set()
    try:  # 'try' starts the block
        # This 'with' block is indented under 'try'
        with open(filename, 'r') as f:
            # This 'for' loop is indented under 'with'
            for line in f:
                # These lines are indented under 'for'
                stripped_id = line.strip()
                if stripped_id:
                    # This line is indented under 'if'
                    ids.add(stripped_id)
        # This log line is indented under 'try', after the 'with' block finishes
        logger.info(f"Loaded {len(ids)} previously posted alert identifiers from {filename}")
    except FileNotFoundError:
        # This 'except' is aligned with 'try'
        logger.info(f"{filename} not found. Starting with an empty set of posted alerts.")
    except IOError as e:
        # This 'except' is aligned with 'try'
        logger.error(f"Error reading persistence file {filename}: {e}")
    # This 'return' is aligned with 'try', indicating it happens after the try/except block
    return ids

def save_posted_ids(ids_set, filename):
    """Saves the set of posted alert IDs to a file."""
    try:
        with open(filename, 'w') as f:
            for identifier in sorted(list(ids_set)): # Save sorted list for readability
                f.write(f"{identifier}\n")
        # logger.debug(f"Saved {len(ids_set)} posted alert IDs to {filename}") # Optional: Log every save
    except IOError as e:
        logger.error(f"Error writing persistence file {filename}: {e}")

# --- GlobalEAS API Interaction ---
async def fetch_active_globaleas_alerts(session, url):
    """Fetches active alerts from the GlobalEAS API."""
    headers = {'Accept': 'application/json'} # Ask for JSON
    logger.info(f"Fetching alerts from: {url}")
    try:
        async with session.get(url, headers=headers, timeout=30) as response:
            response.raise_for_status()
            data = await response.json()
            if isinstance(data, list):
                logger.info(f"Successfully fetched {len(data)} potential alerts.")
                return data
            else:
                logger.error(f"API response was not a list as expected. Type: {type(data)}")
                return []
    except aiohttp.ClientResponseError as e:
        logger.error(f"API HTTP Error: {e.status} {e.message} for URL: {e.request_info.url}")
        try:
             error_body = await e.text(); logger.error(f"API Error Response Body (truncated): {error_body[:500]}")
        except Exception as read_err: logger.error(f"Could not read error response body: {read_err}")
    except Exception as e: logger.error(f"An unexpected error occurred fetching GlobalEAS alerts: {e}", exc_info=True)
    return []

async def download_audio(session, url):
    """Downloads audio file from URL and returns BytesIO object."""
    if not url or not url.startswith(('http://', 'https://')):
        logger.warning(f"Invalid or missing audio URL: {url}"); return None
    try:
        logger.info(f"Attempting to download audio from: {url}")
        async with session.get(url, timeout=45) as response: # Increased timeout for potentially large audio
            response.raise_for_status()
            audio_data = await response.read()
            logger.info(f"Successfully downloaded audio ({len(audio_data)} bytes).")
            return io.BytesIO(audio_data)
    except aiohttp.ClientResponseError as e: logger.error(f"HTTP {e.status} error downloading audio from {url}: {e.message}")
    except asyncio.TimeoutError: logger.error(f"Timeout downloading audio from {url}")
    except Exception as e: logger.error(f"Error downloading audio from {url}: {e}", exc_info=True)
    return None

# --- Main Task Loop ---
@tasks.loop(seconds=CHECK_INTERVAL_SECONDS)
async def check_globaleas_alerts():
    global posted_alert_identifiers
    alert_channel = bot.get_channel(ALERT_CHANNEL_ID)
    log_channel = bot.get_channel(LOG_CHANNEL_ID) if LOG_CHANNEL_ID else None

    if not alert_channel: logger.warning(f"Cannot find alert channel ID: {ALERT_CHANNEL_ID}."); return
    if LOG_CHANNEL_ID and not log_channel: logger.warning(f"Cannot find log channel ID: {LOG_CHANNEL_ID}.")

    async with aiohttp.ClientSession() as session:
        current_alerts = await fetch_active_globaleas_alerts(session, TARGET_API_URL)

    alerts_posted_this_cycle = 0
    for alert_data in current_alerts:
        alert_identifier = alert_data.get("hash")
        if not alert_identifier:
            alert_identifier = f"{alert_data.get('id')}-{alert_data.get('startTimeEpoch')}"
            logger.warning(f"Alert missing 'hash', using fallback identifier: {alert_identifier}")

        event_type = alert_data.get("type", "N/A")

        if alert_identifier in posted_alert_identifiers: continue

        logger.info(f"Detected new GlobalEAS alert to post: {alert_identifier} ({event_type})")
        if log_channel:
             try: await log_channel.send(f"ℹ️ Detected new GlobalEAS alert: `{event_type}` (ID: `{alert_identifier}`). Attempting to post...")
             except Exception as log_e: logger.error(f"Error sending 'Detected' log: {log_e}")

        message = alert_data.get("translation", "No description provided.")
        originator = alert_data.get("originator", "N/A")
        audio_url = alert_data.get("audioUrl") # Get the audio URL
        start_time_str = alert_data.get("startTime") # ISO 8601 format
        end_time_str = alert_data.get("endTime") # ISO 8601 format

        def format_iso_time(time_str):
            if not time_str: return 'N/A'
            try: dt = datetime.fromisoformat(time_str.replace('Z', '+00:00'))
            except ValueError: logger.warning(f"Could not parse ISO date: {time_str}"); return time_str
            return f"<t:{int(dt.timestamp())}:F>"

        start_formatted = format_iso_time(start_time_str)
        end_formatted = format_iso_time(end_time_str)

        embed = discord.Embed(
            title=f"📢 Alert: {event_type} 📢",
            description=message[:2000] + ("..." if len(message)>2000 else ""),
            color=discord.Color.gold()
        )
        embed.add_field(name="Originator", value=originator, inline=True)
        embed.add_field(name="Severity", value=alert_data.get("severity", "N/A"), inline=True)
        embed.add_field(name="Start Time", value=start_formatted, inline=True)
        embed.add_field(name="End Time", value=end_formatted, inline=True)
        embed.set_footer(text=f"Source: alerts.globaleas.org | Hash: {alert_identifier}")
        embed.timestamp = datetime.now(timezone.utc)

        audio_fp = None
        if audio_url:
            async with aiohttp.ClientSession() as audio_session: # Use a session
                 audio_fp = await download_audio(audio_session, audio_url)

        # --- Attempt to Post to Discord --- ## CORRECTED INDENTATION ##
        try:
            # Post the text embed first
            await alert_channel.send(embed=embed)

            # Check if audio was successfully downloaded
            if audio_fp:
                # These lines are indented under 'if audio_fp:'
                filename = "alert_audio.mp3" # Default filename
                try:
                    # Try to get a better filename from the URL
                    # These lines are indented under the inner 'try'
                    potential_name = os.path.basename(urlparse(audio_url).path)
                    if potential_name and '.' in potential_name:
                         # This line is indented under 'if potential_name...'
                         filename = potential_name
                except Exception:
                    # This 'except' is aligned with the inner 'try'
                    pass # Ignore errors getting filename, use default

                # These lines are aligned under 'if audio_fp:', after the inner try-except
                audio_file = discord.File(fp=audio_fp, filename=filename)
                await alert_channel.send(file=audio_file)
                audio_fp.close() # Close the in-memory file

            # --- SUCCESS! Update memory AND save to file ---
            # These lines run if the 'await alert_channel.send(embed=embed)' succeeded
            # and the audio sending part (if applicable) also succeeded or was skipped.
            # They should be indented under the main 'try', aligned with the 'await' above.
            logger.info(f"Successfully posted GlobalEAS alert {alert_identifier} to channel {ALERT_CHANNEL_ID}")
            posted_alert_identifiers.add(alert_identifier) # Add to the in-memory set
            save_posted_ids(posted_alert_identifiers, PERSISTENCE_FILE) # Save the updated set to file
            alerts_posted_this_cycle += 1

            # --- Log Success to Log Channel ---
            if log_channel:
                # This 'if' block is also part of the success sequence, aligned with logger.info etc.
                try:
                    # This line is indented under the inner 'try'
                    await log_channel.send(f"✅ Successfully posted GlobalEAS alert `{event_type}` (ID: `{alert_identifier}`) to #{alert_channel.name}")
                except Exception as log_e:
                    # This 'except' is aligned with the inner 'try'
                    logger.error(f"Error sending 'Success' log: {log_e}")

        # These 'except' blocks are aligned with the main 'try' at the top
        except discord.Forbidden:
            logger.error(f"Permission error posting alert {alert_identifier} to channel {ALERT_CHANNEL_ID}.")
            # Note: ID was not added or saved if posting failed
        except discord.HTTPException as e:
            logger.error(f"Failed to send alert {alert_identifier} (HTTP Error): {e.status} - {e.text}")
        except Exception as e:
            logger.error(f"Unexpected error sending alert {alert_identifier}: {e}", exc_info=True)
        # End of the main try...except block for posting

        if alerts_posted_this_cycle > 0: await asyncio.sleep(3) # Delay between posts

    # if alerts_posted_this_cycle == 0: logger.info("No new alerts found via GlobalEAS API this cycle.")


# --- Setup Before Loop ---
@check_globaleas_alerts.before_loop
async def before_check():
    global posted_alert_identifiers
    await bot.wait_until_ready()
    logger.info("Bot is ready.")
    posted_alert_identifiers = load_posted_ids(PERSISTENCE_FILE)
    logger.info("Starting GlobalEAS API check loop.")

# --- On Ready Event ---
@bot.event
async def on_ready():
    logger.info(f'Logged in as {bot.user.name} ({bot.user.id})')
    logger.info(f'Using GlobalEAS API: {TARGET_API_URL}')
    logger.info(f'Posting alerts to channel ID: {ALERT_CHANNEL_ID}')
    if LOG_CHANNEL_ID: logger.info(f'Logging successful posts to channel ID: {LOG_CHANNEL_ID}')
    else: logger.info('Log channel not configured.')
    logger.info(f'Check interval: {CHECK_INTERVAL_SECONDS} seconds')
    logger.info('------')
    if bot.get_channel(ALERT_CHANNEL_ID) is None: logger.warning(f"Cannot find main alert channel ID {ALERT_CHANNEL_ID}.")
    if LOG_CHANNEL_ID and bot.get_channel(LOG_CHANNEL_ID) is None: logger.warning(f"Cannot find log channel ID {LOG_CHANNEL_ID}.")

    if not check_globaleas_alerts.is_running():
        check_globaleas_alerts.start()

# --- Run the Bot ---
if __name__ == "__main__":
    try:
        bot.run(DISCORD_BOT_TOKEN)
    except Exception as e:
         logger.critical(f"Error running bot: {e}", exc_info=True)
