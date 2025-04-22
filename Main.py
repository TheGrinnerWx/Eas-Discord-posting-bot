# Save this code in a file named EAS.py
# Version: 1.8.1
# Description: A Discord bot that monitors the GlobalEAS API for alerts,
#              posts them with audio to configured channels in multiple servers,
#              and includes setup and utility commands.

# --- Imports ---
# Standard library imports
import json # For handling config file (JSON format)
import os # For accessing environment variables and file paths
import asyncio # For asynchronous operations (e.g., sleep)
from datetime import datetime, timezone # For timestamps
import logging # For logging information and errors
import io # For handling in-memory byte streams (audio data)
from urllib.parse import urlparse # For parsing URLs (extracting filename)
import time # For tracking uptime

# Third-party library imports
import discord # Core discord.py library
from discord.ext import tasks, commands # For background tasks and bot structure
from discord import app_commands # For slash commands
import aiohttp # For making asynchronous HTTP requests
from dotenv import load_dotenv # For loading environment variables from a .env file

# --- Load Environment Variables ---
# Loads variables from a .env file in the same directory if it exists.
# This is useful for development and doesn't require setting system-wide variables.
# Create a file named '.env' and add DISCORD_BOT_TOKEN="YOUR_TOKEN_HERE"
load_dotenv()

# --- Configuration Constants ---
# Get the bot token from environment variables (this is REQUIRED)
DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
# Get the desired check interval in seconds, default to 120 (2 minutes)
CHECK_INTERVAL_SECONDS_STR = os.getenv('CHECK_INTERVAL_SECONDS', '120')
# API endpoint for fetching active alerts from GlobalEAS
TARGET_API_URL = "https://alerts.globaleas.org/api/v1/alerts/active"
# File to store hashes of posted alerts to prevent duplicates across restarts
PERSISTENCE_FILE = "posted_globaleas_alerts.txt"
# File to store configured channel IDs for each server (guild)
CONFIG_FILE = "config.json"
# Minimum allowed check interval in seconds to prevent API spam
MIN_CHECK_INTERVAL = 30
# Maximum number of alerts to show in the /alerts command embed
MAX_ALERTS_TO_SHOW = 10
# Default audio filename if one cannot be extracted from the URL
DEFAULT_AUDIO_FILENAME = "alert_audio.mp3"
# Timeout for API requests in seconds
API_TIMEOUT_SECONDS = 30
# Timeout for audio downloads in seconds (can be longer)
AUDIO_DOWNLOAD_TIMEOUT_SECONDS = 45
# Small delay between posting multiple alerts in one cycle (seconds)
POST_DELAY_SECONDS = 1

# --- Validate Essential Configuration ---
# The bot cannot run without a token.
if not DISCORD_BOT_TOKEN:
    print("CRITICAL: DISCORD_BOT_TOKEN environment variable not set. Bot cannot start.")
    # Exit the script immediately if the token is missing.
    exit()

# Validate the check interval value provided by the user or use the default.
try:
    CHECK_INTERVAL_SECONDS = int(CHECK_INTERVAL_SECONDS_STR)
    # Enforce a minimum check interval.
    if CHECK_INTERVAL_SECONDS < MIN_CHECK_INTERVAL:
        print(f"WARNING: CHECK_INTERVAL_SECONDS is below minimum ({MIN_CHECK_INTERVAL}s). Setting interval to {MIN_CHECK_INTERVAL} seconds.")
        CHECK_INTERVAL_SECONDS = MIN_CHECK_INTERVAL
except ValueError:
    # Warn only if the user provided an invalid value, not if it was missing (using default).
    if os.getenv('CHECK_INTERVAL_SECONDS') is not None:
         print(f"WARNING: CHECK_INTERVAL_SECONDS ('{CHECK_INTERVAL_SECONDS_STR}') is not a valid integer. Using default 120 seconds.")
    CHECK_INTERVAL_SECONDS = 120

# --- Logging Setup ---
# Configure basic logging to print informational messages and errors to the console.
# Includes timestamp, log level, logger name, and the message.
logging.basicConfig(
    level=logging.INFO, # Set the minimum level of messages to log
    format='%(asctime)s:%(levelname)-8s:%(name)-15s: %(message)s', # Define the log message format
    datefmt='%Y-%m-%d %H:%M:%S' # Define the timestamp format
)
# Get a logger instance specific to this bot module.
logger = logging.getLogger('GlobalEASBot')

# --- Bot Setup ---
# Define necessary intents. Default intents are usually sufficient for slash commands and basic events.
# These are usually sufficient for slash commands and accessing basic guild/channel information without needing privileged intents.
# If you needed message content or member lists, you'd enable them here and in the Developer Portal.
intents = discord.Intents.default()
# Create the Bot instance. The command prefix isn't strictly necessary if only using slash commands,
# but discord.py requires it. We won't be using prefix commands in this script.
bot = commands.Bot(command_prefix="!", intents=intents)

# --- Global Variables ---
# Dictionary to store configurations for each guild the bot is in.
# Structure: {guild_id (int): {"alert_channel_id": int, "log_channel_id": int or None}}
guild_configs = {}
# Set to store unique identifiers (hashes) of alerts already processed by the bot globally.
# This is loaded from and saved to PERSISTENCE_FILE.
posted_alert_identifiers = set()
# Track bot start time for uptime calculation in the /status command.
bot_start_time = time.time()
# Counter for total alerts posted since the bot last started. Resets on restart.
total_alerts_posted_session = 0

# --- Persistence Functions ---
# Functions to load and save the set of posted alert hashes from/to a file.

def load_posted_ids(filename=PERSISTENCE_FILE):
    """Loads posted alert IDs (hashes) from a file into a set."""
    ids = set()
    logger.debug(f"Attempting to load posted IDs from {filename}")
    try:
        # Open the persistence file in read mode ('r')
        with open(filename, 'r', encoding='utf-8') as f:
            # Iterate through each line in the file
            for line_num, line in enumerate(f, 1):
                # Remove leading/trailing whitespace from the line
                stripped_id = line.strip()
                # If the line wasn't empty, add the ID to the set
                if stripped_id:
                    ids.add(stripped_id)
                # else: logger.warning(f"Skipping empty line {line_num} in {filename}") # Optional: More verbose
        logger.info(f"Loaded {len(ids)} previously posted alert identifiers from {filename}")
    except FileNotFoundError:
        # This is expected on the first run or if the file is deleted
        logger.info(f"Persistence file '{filename}' not found. Starting with an empty set.")
    except IOError as e:
        # Log errors encountered during file reading
        logger.error(f"Error reading persistence file {filename}: {e}")
    except Exception as e:
        # Catch any other unexpected errors during loading
        logger.error(f"Unexpected error loading persistence file {filename}: {e}", exc_info=True)
    # Return the populated set (or an empty one if file not found/error)
    return ids

def save_posted_ids(ids_set, filename=PERSISTENCE_FILE):
    """Saves the current set of posted alert IDs (hashes) to a file."""
    logger.debug(f"Attempting to save {len(ids_set)} posted IDs to {filename}")
    temp_filename = filename + ".tmp" # Use a temporary file for atomic write
    try:
        # Open a temporary file for writing ('w'), which overwrites the file
        with open(temp_filename, 'w', encoding='utf-8') as f:
            # Sort the IDs before writing for consistency and easier manual inspection
            # Write each identifier followed by a newline character
            for identifier in sorted(list(ids_set)):
                f.write(f"{identifier}\n")
        # Atomically replace the old file with the new one
        os.replace(temp_filename, filename)
        logger.debug(f"Successfully saved {len(ids_set)} posted alert IDs to {filename}")
    except IOError as e:
        # Log errors encountered during file writing
        logger.error(f"Error writing persistence file {filename}: {e}")
        # Clean up temp file if it exists on error
        if os.path.exists(temp_filename):
            try: os.remove(temp_filename)
            except OSError as rm_err: logger.error(f"Could not remove temporary file {temp_filename}: {rm_err}")
    except Exception as e:
        logger.error(f"Unexpected error saving persistence file {filename}: {e}", exc_info=True)
        if os.path.exists(temp_filename):
            try: os.remove(temp_filename)
            except OSError as rm_err: logger.error(f"Could not remove temporary file {temp_filename}: {rm_err}")


# --- Config File Functions (Multi-Guild) ---
# Functions to load and save the per-guild channel configurations from/to a JSON file.

def load_config(filename=CONFIG_FILE):
    """Loads guild configurations from a JSON file."""
    global guild_configs # Modify the global dictionary
    logger.debug(f"Attempting to load guild configurations from {filename}")
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            loaded_data = json.load(f) # Load the entire JSON structure

        # Basic validation: Ensure the loaded data is a dictionary
        if not isinstance(loaded_data, dict):
            logger.error(f"Config file {filename} is not a valid JSON dictionary. Resetting config.")
            guild_configs = {} # Reset in-memory config
            return False

        valid_configs = {}
        # Iterate through the guild IDs (keys) and their configs (values)
        for guild_id_str, config in loaded_data.items():
            try:
                guild_id = int(guild_id_str) # Keys in JSON are strings, convert to int
                # Validate the structure of the config for each guild
                if isinstance(config, dict) and 'alert_channel_id' in config:
                     alert_ch = config.get('alert_channel_id')
                     log_ch = config.get('log_channel_id') # Optional
                     # Ensure channel IDs are integers or None before storing
                     valid_configs[guild_id] = {
                         'alert_channel_id': int(alert_ch) if alert_ch is not None else None,
                         'log_channel_id': int(log_ch) if log_ch is not None else None
                     }
                else:
                     logger.warning(f"Skipping invalid config entry for guild '{guild_id_str}' in {filename}. Missing 'alert_channel_id' or not a dictionary.")
            except (ValueError, TypeError):
                # Handle cases where guild ID isn't an int or channel IDs aren't ints/None
                logger.warning(f"Skipping invalid data types for guild ID or channel IDs for key '{guild_id_str}' in {filename}")

        # Update the global dictionary with validated configurations
        guild_configs = valid_configs
        logger.info(f"Loaded configurations for {len(guild_configs)} guilds from {filename}")
        return True # Indicate successful load

    except FileNotFoundError:
        # Expected case if the bot hasn't been configured in any server yet
        logger.warning(f"{filename} not found. Use /setup in each server to configure.")
        guild_configs = {} # Start with empty config if file doesn't exist
    except json.JSONDecodeError as e:
        # Handle errors if the JSON file is corrupted or invalid
        logger.error(f"Error decoding {filename}: {e}. Check file format or delete it and re-run /setup.")
        guild_configs = {} # Reset config on decode error
    except Exception as e:
        # Catch any other unexpected errors during loading
        logger.error(f"Unexpected error loading config from {filename}: {e}", exc_info=True)
        guild_configs = {} # Reset config on other errors
    return False # Indicate failure

def save_config(filename=CONFIG_FILE):
    """Saves the current guild configurations to the config file."""
    global guild_configs
    logger.debug(f"Attempting to save configurations for {len(guild_configs)} guilds to {filename}")
    # Convert integer guild IDs back to strings for JSON keys before saving
    configs_to_save = {str(gid): data for gid, data in guild_configs.items()}
    temp_filename = filename + ".tmp" # Use temporary file for atomic write
    try:
        # Write dictionary to temporary JSON file with pretty-printing
        with open(temp_filename, 'w', encoding='utf-8') as f:
            json.dump(configs_to_save, f, indent=4)
        # Replace the actual config file with the temporary one
        os.replace(temp_filename, filename)
        logger.info(f"Configuration saved successfully to {filename}")
    except IOError as e:
        logger.error(f"Error writing config file {filename}: {e}")
        if os.path.exists(temp_filename):
            try: os.remove(temp_filename)
            except OSError as rm_err: logger.error(f"Could not remove temp config file {temp_filename}: {rm_err}")
    except Exception as e:
        logger.error(f"Unexpected error saving config to {filename}: {e}", exc_info=True)
        if os.path.exists(temp_filename):
            try: os.remove(temp_filename)
            except OSError as rm_err: logger.error(f"Could not remove temp config file {temp_filename}: {rm_err}")


# --- GlobalEAS API Interaction ---
async def fetch_active_globaleas_alerts(session, url=TARGET_API_URL):
    """Fetches active alerts from the GlobalEAS API."""
    headers = {'Accept': 'application/json'} # Specify desired response format
    logger.info(f"Fetching alerts from: {url}")
    try:
        async with session.get(url, headers=headers, timeout=API_TIMEOUT_SECONDS) as response:
            response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)
            data = await response.json() # Parse the JSON body
            # Expecting the API to return a list of alert objects
            if isinstance(data, list):
                logger.info(f"Successfully fetched {len(data)} potential alerts from API.")
                return data
            else:
                logger.error(f"API response was not a list as expected. Received type: {type(data)}. Response: {str(data)[:500]}")
                return []
    except aiohttp.ClientResponseError as e:
        logger.error(f"API HTTP Error: {e.status} {e.message} for URL: {e.request_info.url}")
        try:
            error_body = await e.text(encoding='utf-8', errors='ignore') # Attempt to read error body
            logger.error(f"API Error Response Body (truncated): {error_body[:500]}")
        except Exception:
            logger.error("Could not read error response body.")
    except aiohttp.ClientConnectionError as e:
        logger.error(f"Network connection error fetching alerts: {e}")
    except asyncio.TimeoutError:
        logger.error(f"Timeout fetching alerts from GlobalEAS API ({API_TIMEOUT_SECONDS}s).")
    except json.JSONDecodeError as e:
        logger.error(f"Error decoding JSON response from GlobalEAS API: {e}")
    except Exception as e:
        logger.error(f"An unexpected error occurred fetching GlobalEAS alerts: {e}", exc_info=True)
    return [] # Return empty list on any error

async def download_audio(session, url):
    """Downloads audio file from URL and returns BytesIO object."""
    # Basic validation: Check if URL is present and looks like HTTP/HTTPS
    if not url or not url.startswith(('http://', 'https://')):
        logger.warning(f"Invalid or missing audio URL provided for download: {url}")
        return None
    try:
        logger.info(f"Attempting to download audio from: {url}")
        async with session.get(url, timeout=AUDIO_DOWNLOAD_TIMEOUT_SECONDS) as response: # Longer timeout for audio files
            response.raise_for_status() # Check for download errors
            audio_data = await response.read() # Read the entire audio content as bytes
            if not audio_data:
                 logger.warning(f"Downloaded audio from {url} but content is empty.")
                 return None
            logger.info(f"Successfully downloaded audio ({len(audio_data)} bytes) from {url}.")
            return io.BytesIO(audio_data) # Return as an in-memory bytes stream
    except aiohttp.ClientResponseError as e:
         logger.error(f"HTTP {e.status} error downloading audio from {url}: {e.message}")
    except asyncio.TimeoutError:
         logger.error(f"Timeout downloading audio from {url} ({AUDIO_DOWNLOAD_TIMEOUT_SECONDS}s).")
    except Exception as e:
        logger.error(f"Unexpected error downloading audio from {url}: {e}", exc_info=True)
    return None # Return None if any error occurs

# --- Discord Commands ---
# Define slash commands for interaction within Discord.

# Setup Command
@bot.tree.command(name="setup", description="Configure the alert and log channels for THIS server.")
@app_commands.describe(
    alerts_channel="The text channel where EAS alerts should be posted.",
    logs_channel="Optional: The text channel for status/success logs."
)
@app_commands.checks.has_permissions(administrator=True) # Only admins can configure
@app_commands.guild_only() # Command must be used in a server context
async def setup(interaction: discord.Interaction, alerts_channel: discord.TextChannel, logs_channel: discord.TextChannel = None):
    """Command handler for the /setup slash command."""
    global guild_configs # Access the global config dictionary
    # Interaction should always have a guild due to the decorator, but check defensively
    if not interaction.guild:
        await interaction.response.send_message("This command can only be used within a server.", ephemeral=True); return
    guild_id = interaction.guild.id
    # Acknowledge the command quickly and make the final response ephemeral (only visible to user)
    await interaction.response.defer(ephemeral=True)

    # Update the configuration dictionary for this specific guild
    guild_configs[guild_id] = {
        'alert_channel_id': alerts_channel.id,
        'log_channel_id': logs_channel.id if logs_channel else None
    }
    # Save the entire updated config dictionary to the JSON file
    save_config(CONFIG_FILE)

    # Create and send a confirmation message
    log_msg = f"Log channel set to {logs_channel.mention} (`{logs_channel.id}`)." if logs_channel else "Log channel **not set**."
    confirmation_message = (
        f"✅ Configuration for **{interaction.guild.name}** updated!\n"
        f"🔹 Alerts channel: {alerts_channel.mention} (`{alerts_channel.id}`).\n"
        f"🔹 {log_msg}"
    )
    await interaction.followup.send(confirmation_message, ephemeral=True)
    # Log the configuration change to the console
    logger.info(f"Config updated via /setup for Guild {guild_id} by {interaction.user}. Alert: {alerts_channel.id}, Log: {logs_channel.id if logs_channel else None}")

@setup.error
async def setup_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """Error handler specifically for the /setup command."""
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("❌ You need **Administrator** permissions to run this command.", ephemeral=True)
    elif isinstance(error, app_commands.NoPrivateMessage):
         await interaction.response.send_message("❌ This command can only be used within a server.", ephemeral=True)
    else:
        logger.error(f"Error in /setup command for Guild {interaction.guild_id}: {error}", exc_info=True)
        error_message = "❌ An unexpected error occurred while setting up. Please check bot logs."
        # Use followup if response already deferred
        if interaction.response.is_done(): await interaction.followup.send(error_message, ephemeral=True)
        else: await interaction.response.send_message(error_message, ephemeral=True)

# Fetch Command
@bot.tree.command(name="fetch", description="Manually trigger an immediate check for new alerts.")
@app_commands.checks.has_permissions(administrator=True) # Only admins can trigger manual fetch
@app_commands.guild_only() # Only usable in a server context
async def fetch(interaction: discord.Interaction):
    """Command handler for the /fetch slash command."""
    await interaction.response.defer(ephemeral=True) # Acknowledge privately
    logger.info(f"/fetch command triggered by {interaction.user} ({interaction.user.id}) in Guild {interaction.guild_id}")

    # Check if the background task is currently running
    if check_globaleas_alerts.is_running() and not check_globaleas_alerts.is_being_cancelled():
        try:
            logger.info("Manual fetch: Calling check_globaleas_alerts() task function.")
            # Execute the main checking function once, immediately.
            # This will fetch alerts and post to all configured guilds if new alerts are found.
            await check_globaleas_alerts()
            await interaction.followup.send("✅ Manual alert check initiated. New alerts (if any) have been processed.", ephemeral=True)
        except Exception as e:
            logger.error(f"Error during manual fetch execution triggered by {interaction.user}: {e}", exc_info=True)
            await interaction.followup.send("❌ An error occurred while trying to fetch alerts.", ephemeral=True)
    else:
        # This case might happen if the task failed to start or was stopped
        await interaction.followup.send("⚠️ The alert checking task is not currently running. Please wait or check bot logs.", ephemeral=True)

@fetch.error
async def fetch_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """Error handler for the /fetch command."""
    if isinstance(error, app_commands.MissingPermissions): await interaction.response.send_message("❌ Need Admin perms.", ephemeral=True)
    elif isinstance(error, app_commands.NoPrivateMessage): await interaction.response.send_message("❌ Use in a server.", ephemeral=True)
    else: logger.error(f"Error in /fetch command: {error}", exc_info=True); await interaction.followup.send("❌ An error occurred.", ephemeral=True) if interaction.response.is_done() else await interaction.response.send_message("❌ An error occurred.", ephemeral=True)

# Alerts Command
@bot.tree.command(name="alerts", description="Show currently active alerts reported by the API.")
async def alerts(interaction: discord.Interaction):
    """Command handler for the /alerts slash command."""
    # Defer response, make it visible to the channel
    await interaction.response.defer(ephemeral=False)
    logger.info(f"/alerts command triggered by {interaction.user} ({interaction.user.id}) in Guild {interaction.guild_id}")

    alerts_list = []
    # Fetch alerts directly from the API for this command
    async with aiohttp.ClientSession() as session:
        alerts_list = await fetch_active_globaleas_alerts(session, TARGET_API_URL)

    # Handle case where no alerts are found
    if not alerts_list:
        await interaction.followup.send("ℹ️ No active alerts found from the GlobalEAS API right now.", ephemeral=False)
        return

    # Create an embed to display the alert summary
    embed = discord.Embed(
        title=f"🚨 Currently Active Alerts ({len(alerts_list)} found)",
        description=f"Showing the first {MAX_ALERTS_TO_SHOW} active alerts from GlobalEAS API:",
        color=discord.Color.blue() # Use blue for informational display
    )

    count = 0
    # Loop through fetched alerts, adding info to the embed
    for alert in alerts_list:
        # Stop after adding MAX_ALERTS_TO_SHOW alerts
        if count >= MAX_ALERTS_TO_SHOW:
            embed.description += f"\n...and {len(alerts_list) - count} more."
            break

        # Extract key information for the summary field
        event_type = alert.get("type", "N/A")
        originator = alert.get("originator", "N/A")
        severity = alert.get("severity", "N/A")
        alert_hash = alert.get('hash', 'N/A') # The unique ID we use

        # Add a field to the embed for this alert
        embed.add_field(
            name=f"{count+1}. {event_type} ({severity})",
            value=f"Originator: {originator}\nHash: `{alert_hash}`",
            inline=False # Display each alert in its own block
        )
        count += 1

    embed.set_footer(text="Source: alerts.globaleas.org")
    embed.timestamp = datetime.now(timezone.utc) # Add timestamp to the embed

    # Send the summary embed as a follow-up message
    await interaction.followup.send(embed=embed, ephemeral=False)


@alerts.error
async def alerts_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """Error handler for the /alerts command."""
    logger.error(f"Error in /alerts command: {error}", exc_info=True)
    error_message = "❌ An unexpected error occurred while fetching alerts."
    if interaction.response.is_done(): await interaction.followup.send(error_message, ephemeral=True)
    else: await interaction.response.send_message(error_message, ephemeral=True)

# Config Command
@bot.tree.command(name="config", description="Show the current alert/log channel config for this server.")
@app_commands.checks.has_permissions(administrator=True) # Only admins can view config
@app_commands.guild_only()
async def config_command(interaction: discord.Interaction):
    """Displays the current configuration for the server where the command is run."""
    global guild_configs
    if not interaction.guild: await interaction.response.send_message("❌ Use in a server.", ephemeral=True); return
    guild_id = interaction.guild.id
    await interaction.response.defer(ephemeral=True) # Respond privately

    # Get the configuration for the current guild from the global dictionary
    server_config = guild_configs.get(guild_id)

    # Format the response message based on whether config exists
    if server_config and server_config.get('alert_channel_id'):
        alert_ch_id = server_config['alert_channel_id']
        log_ch_id = server_config.get('log_channel_id')
        # Use channel mentions for better UX
        alert_ch_mention = f"<#{alert_ch_id}> (`{alert_ch_id}`)"
        log_ch_mention = f"<#{log_ch_id}> (`{log_ch_id}`)" if log_ch_id else "**Not Set**"
        message = (
            f"ℹ️ Current configuration for **{interaction.guild.name}**:\n"
            f"🔹 Alert Channel: {alert_ch_mention}\n"
            f"🔹 Log Channel: {log_ch_mention}"
        )
    else:
        message = f"⚠️ No configuration found for **{interaction.guild.name}**. Use `/setup` to configure."

    await interaction.followup.send(message, ephemeral=True)

@config_command.error
async def config_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """Error handler for the /config command."""
    if isinstance(error, app_commands.MissingPermissions): await interaction.response.send_message("❌ Need Admin perms.", ephemeral=True)
    elif isinstance(error, app_commands.NoPrivateMessage): await interaction.response.send_message("❌ Use in a server.", ephemeral=True)
    else: logger.error(f"Error in /config: {error}", exc_info=True); await interaction.followup.send("❌ An error occurred.", ephemeral=True) if interaction.response.is_done() else await interaction.response.send_message("❌ An error occurred.", ephemeral=True)

# Status Command
@bot.tree.command(name="status", description="Show bot status and statistics.")
async def status_command(interaction: discord.Interaction):
    """Displays bot status information like uptime and latency."""
    global bot_start_time, total_alerts_posted_session, guild_configs
    # Respond publicly
    await interaction.response.defer(ephemeral=False)

    # Calculate uptime
    current_time = time.time()
    uptime_seconds = int(current_time - bot_start_time)
    # Format uptime into days, hours, minutes, seconds
    days, rem = divmod(uptime_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    uptime_str = f"{days}d {hours}h {minutes}m {seconds}s" if days > 0 else f"{hours}h {minutes}m {seconds}s"

    # Create status embed
    embed = discord.Embed(title="📊 Bot Status", color=discord.Color.light_grey())
    embed.add_field(name="Uptime", value=uptime_str, inline=True)
    embed.add_field(name="Latency", value=f"{bot.latency * 1000:.2f} ms", inline=True)
    embed.add_field(name="Configured Servers", value=f"{len(guild_configs)}", inline=True)
    embed.add_field(name="Alerts Posted (Session)", value=f"{total_alerts_posted_session}", inline=True)
    embed.timestamp = datetime.now(timezone.utc)

    await interaction.followup.send(embed=embed, ephemeral=False)

@status_command.error
async def status_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """Error handler for the /status command."""
    logger.error(f"Error in /status command: {error}", exc_info=True)
    error_message = "❌ An unexpected error occurred while getting status."
    if interaction.response.is_done(): await interaction.followup.send(error_message, ephemeral=True)
    else: await interaction.response.send_message(error_message, ephemeral=True)

# Help Command
@bot.tree.command(name="help", description="Shows information about available commands.")
async def help_command(interaction: discord.Interaction):
    """Displays help information for the bot's commands."""
    # Create help embed
    embed = discord.Embed(
        title="GlobalEAS Bot Help",
        description="Monitors GlobalEAS API for alerts and posts them to configured servers.",
        color=discord.Color.green()
    )
    # Add fields for each command
    embed.add_field(name="/setup `alerts_channel` `[logs_channel]`", value="(Admin Only) Sets alert/log channels **for the current server**.", inline=False)
    embed.add_field(name="/fetch", value="(Admin Only) Manually triggers an immediate check for new alerts.", inline=False)
    embed.add_field(name="/alerts", value="Displays a summary of currently active alerts found via the API.", inline=False)
    embed.add_field(name="/config", value="(Admin Only) Shows the current alert/log channel config for this server.", inline=False)
    embed.add_field(name="/status", value="Shows bot uptime and basic statistics.", inline=False)
    embed.set_footer(text="Run /setup in each server where you want alerts.")
    # Send the help message privately to the user who asked
    await interaction.response.send_message(embed=embed, ephemeral=True)


# --- Main Task Loop (Multi-Guild) ---
@tasks.loop(seconds=CHECK_INTERVAL_SECONDS)
async def check_globaleas_alerts():
    """Periodically checks the GlobalEAS API and posts new alerts to configured guilds."""
    global posted_alert_identifiers, guild_configs, total_alerts_posted_session

    # If no servers have been configured via /setup, don't do anything
    if not guild_configs:
        logger.debug("No guilds configured via /setup yet. Skipping alert check.")
        return

    # --- Fetch Alerts ONCE ---
    # Fetch the list of currently active alerts from the API
    async with aiohttp.ClientSession() as session:
        fetched_alerts = await fetch_active_globaleas_alerts(session, TARGET_API_URL)

    # --- Filter out already processed alerts ---
    # Create a list to hold alerts that haven't been posted yet
    new_alerts = []
    for alert_data in fetched_alerts:
        # Use the 'hash' field as the unique identifier, create fallback if needed
        alert_identifier = alert_data.get("hash")
        if not alert_identifier:
            alert_identifier = f"{alert_data.get('id')}-{alert_data.get('startTimeEpoch')}"
            # Log if hash is missing, as it's preferred
            logger.warning(f"Alert missing 'hash', using fallback identifier: {alert_identifier}")

        # If this identifier is not in our set of already posted alerts, add it to the list to process
        if alert_identifier not in posted_alert_identifiers:
            new_alerts.append((alert_identifier, alert_data)) # Store as (identifier, data) tuple
        # else: logger.debug(f"Skipping already processed alert: {alert_identifier}") # Optional verbose log

    # If there are no new alerts, exit the function for this cycle
    if not new_alerts:
        # logger.info("No new alerts found via GlobalEAS API this cycle.") # Can be noisy
        return

    logger.info(f"Found {len(new_alerts)} new alerts to process for {len(guild_configs)} configured guilds.")

    # --- Process each NEW alert ---
    for alert_identifier, alert_data in new_alerts:
        event_type = alert_data.get("type", "N/A") # e.g., "TOR", "SVR"

        # --- Prepare Embed and Audio Data ONCE per alert ---
        # This prevents redundant processing for each server
        message = alert_data.get("translation", "No description provided.")
        originator = alert_data.get("originator", "N/A")
        audio_url = alert_data.get("audioUrl")
        start_time_str = alert_data.get("startTime")
        end_time_str = alert_data.get("endTime")
        alert_id_num = alert_data.get("id", "N/A") # Numeric ID for footer

        # Helper function to format ISO timestamp strings for Discord
        def format_iso_time(time_str):
            if not time_str: return 'N/A'
            try: dt = datetime.fromisoformat(time_str.replace('Z', '+00:00'))
            except ValueError: logger.warning(f"Could not parse ISO date string: {time_str}"); return time_str
            return f"<t:{int(dt.timestamp())}:F>" # Discord timestamp format

        start_formatted = format_iso_time(start_time_str)
        end_formatted = format_iso_time(end_time_str)

        # Create the main embed for the alert
        embed = discord.Embed(
            title=f"📢 Alert: {event_type} 📢",
            description=message[:2000] + ("..." if len(message)>2000 else ""), # Truncate long messages
            color=discord.Color.gold() # Use a distinct color
        )
        embed.add_field(name="Originator", value=originator, inline=True)
        embed.add_field(name="Severity", value=alert_data.get("severity", "N/A"), inline=True)
        embed.add_field(name="Start Time", value=start_formatted, inline=True)
        embed.add_field(name="End Time", value=end_formatted, inline=True)
        embed.set_footer(text=f"ID: {alert_id_num} | Alert: {event_type}") # Use requested footer format
        embed.timestamp = datetime.now(timezone.utc) # Add current time as embed timestamp

        # Download audio ONCE if URL exists
        audio_data_bytes = None # Store raw audio bytes
        audio_filename = DEFAULT_AUDIO_FILENAME # Default filename
        if audio_url:
            async with aiohttp.ClientSession() as audio_session:
                 audio_fp = await download_audio(audio_session, audio_url)
            if audio_fp:
                audio_data_bytes = audio_fp.getvalue() # Read bytes from BytesIO
                audio_fp.close() # Close the BytesIO object
                # Try to get a better filename from the URL
                try:
                    parsed_url = urlparse(audio_url)
                    potential_name = os.path.basename(parsed_url.path)
                    if potential_name and '.' in potential_name:
                        audio_filename = potential_name
                except Exception as e:
                    logger.warning(f"Could not parse filename from audio URL {audio_url}: {e}")

        # --- Post the alert to ALL configured guilds ---
        posted_to_any_guild = False # Track if posting succeeded anywhere
        # Create a copy of keys to iterate over safely, in case guild_configs is modified by /setup during iteration
        guild_ids_to_process = list(guild_configs.keys())

        for guild_id in guild_ids_to_process:
            config = guild_configs.get(guild_id) # Get current config for this guild
            # Defensive check in case config was removed mid-loop
            if not config:
                logger.warning(f"Config for Guild {guild_id} removed during processing loop. Skipping.")
                continue

            alert_channel_id = config.get('alert_channel_id')
            log_channel_id = config.get('log_channel_id')

            # Skip if no alert channel is configured for this guild
            if not alert_channel_id:
                logger.warning(f"Guild {guild_id} is missing alert_channel_id in config. Skipping.")
                continue

            # Get the actual channel object from the ID
            alert_channel = bot.get_channel(alert_channel_id)
            if not alert_channel:
                logger.warning(f"Cannot find alert channel {alert_channel_id} for Guild {guild_id}. Skipping.")
                continue

            # Get log channel object if configured
            log_channel = bot.get_channel(log_channel_id) if log_channel_id else None
            if log_channel_id and not log_channel:
                logger.warning(f"Cannot find configured log channel {log_channel_id} for Guild {guild_id}.")

            # Log detection to this specific guild's log channel
            if log_channel:
                 try:
                     await log_channel.send(f"ℹ️ Processing alert: `{event_type}` (ID: `{alert_identifier}`)")
                 except Exception as log_e:
                     logger.error(f"Log send error (Guild {guild_id}, Channel {log_channel_id}): {log_e}")

            # --- Attempt to Post to this Guild's Alert Channel ---
            try:
                # Prepare a new discord.File object for each send if audio exists
                # We need to create a new BytesIO stream from the stored bytes for each send
                discord_file = discord.File(fp=io.BytesIO(audio_data_bytes), filename=audio_filename) if audio_data_bytes else None

                # Send the message (embed + optional file)
                if discord_file:
                    await alert_channel.send(embed=embed, file=discord_file)
                    logger.info(f"Sent alert WITH audio to #{alert_channel.name} (Guild {guild_id})")
                else:
                    await alert_channel.send(embed=embed)
                    logger.info(f"Sent alert (no audio) to #{alert_channel.name} (Guild {guild_id})")

                # Mark that we successfully posted this alert somewhere
                posted_to_any_guild = True

                # Log success to this guild's log channel
                if log_channel:
                    try:
                        await log_channel.send(f"✅ Posted: `{event_type}` (ID: `{alert_identifier}`) to #{alert_channel.name}")
                    except Exception as log_e:
                        logger.error(f"Log send error (Guild {guild_id}, Channel {log_channel_id}): {log_e}")

            # --- Handle Posting Errors for this specific guild ---
            except discord.Forbidden:
                logger.error(f"Permission error: Cannot post to #{alert_channel.name} (Guild {guild_id}). Check bot permissions in this channel/server.")
            except discord.HTTPException as e:
                logger.error(f"Discord HTTP Error posting to #{alert_channel.name} (Guild {guild_id}): {e.status} - {e.text}")
            except Exception as e:
                logger.error(f"Unexpected error sending to #{alert_channel.name} (Guild {guild_id}): {e}", exc_info=True)

        # --- Update Persistence ONLY if posted successfully to at least one guild ---
        # This prevents marking an alert as "posted" if it failed everywhere due to permissions etc.
        if posted_to_any_guild:
            logger.info(f"Finished processing alert {alert_identifier}. Adding to globally posted list.")
            posted_alert_identifiers.add(alert_identifier)
            save_posted_ids(posted_alert_identifiers, PERSISTENCE_FILE)
            total_alerts_posted_session += 1 # Increment session counter
        else:
             # If it failed everywhere, log a warning. It will be retried next cycle.
             logger.warning(f"Alert {alert_identifier} ({event_type}) was not posted to any configured guild successfully.")

        # Small delay between processing *different* new alerts in the same cycle to avoid rate limits
        await asyncio.sleep(POST_DELAY_SECONDS)


# --- Setup Before Loop ---
@check_globaleas_alerts.before_loop
async def before_check():
    """Runs once before the task loop starts."""
    global posted_alert_identifiers
    await bot.wait_until_ready() # Wait for the bot to be fully connected
    logger.info("Bot is ready. Loading configuration and persistence...")
    load_config(CONFIG_FILE) # Load guild channel configurations
    posted_alert_identifiers = load_posted_ids(PERSISTENCE_FILE) # Load previously posted alert hashes
    logger.info("Initialization complete. Starting GlobalEAS API check loop.")

# --- On Ready Event ---
@bot.event
async def on_ready():
    """Runs when the bot successfully connects to Discord and is ready."""
    global bot_start_time
    bot_start_time = time.time() # Record start time for uptime calculation
    logger.info(f'Logged in as {bot.user.name} ({bot.user.id})')
    logger.info(f'discord.py version: {discord.__version__}')
    logger.info(f'Using GlobalEAS API: {TARGET_API_URL}')

    # Load config initially and log status
    load_config(CONFIG_FILE)
    logger.info(f"Loaded config for {len(guild_configs)} guilds.")
    if not guild_configs:
        logger.warning("No server configurations loaded. Use /setup in desired servers.")

    logger.info(f'Check interval set to: {CHECK_INTERVAL_SECONDS} seconds')
    logger.info('------')

    # Sync application commands (slash commands) with Discord
    try:
        # Sync globally. Can take up to an hour for new/changed commands to show up.
        # For faster testing during development, sync to a specific server (guild):
        # test_guild_id = 123456789012345678 # Replace with your server ID
        # synced = await bot.tree.sync(guild=discord.Object(id=test_guild_id))
        synced = await bot.tree.sync() # Sync globally
        logger.info(f"Synced {len(synced)} application commands.")
    except Exception as e:
        logger.error(f"Failed to sync application commands: {e}")

    # Start the main alert checking background task if it's not already running
    if not check_globaleas_alerts.is_running():
        logger.info("Starting the check_globaleas_alerts task loop.")
        check_globaleas_alerts.start()
    else:
        # This case shouldn't normally happen unless the bot logic is complex/restarted internally
        logger.warning("check_globaleas_alerts task loop was already running.")


# --- Run the Bot ---
# This block executes only when the script is run directly (e.g., python EAS.py)
if __name__ == "__main__":
    # Final check for the token before attempting to run
    if not DISCORD_BOT_TOKEN:
        logger.critical("DISCORD_BOT_TOKEN environment variable is missing. Bot cannot start.")
    else:
        # Use a try-except block to catch errors during bot execution
        try:
            # Start the bot using the token from the environment variable
            logger.info("Attempting to run the bot...")
            bot.run(DISCORD_BOT_TOKEN)
        # Handle specific common errors during login/startup
        except discord.LoginFailure:
             logger.critical("Login failed: Invalid Discord Bot Token provided.")
        except discord.PrivilegedIntentsRequired:
             logger.critical("Login failed: Privileged Intents (like Members or Presence) might be required but are not enabled in the Developer Portal.")
        except Exception as e:
             # Catch any other exceptions during bot startup or runtime
             logger.critical(f"An error occurred while running the bot: {e}", exc_info=True)
