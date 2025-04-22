# Discord Alert Bot

A Python-based Discord bot that fetches alerts and corresponding audio from the GlobalEAS API (`https://alerts.globaleas.org/api/v1/alerts/active`) and posts them to a specified Discord channel. It uses persistence to avoid reposting alerts after restarts.

## Features

* Polls the GlobalEAS API for active alerts periodically.
* Parses alert details (event type, message, times, originator).
* Downloads associated audio file if available via the API's `audioUrl` field.
* Posts alerts as formatted embeds to a Discord channel.
* Posts the downloaded audio as a separate file.
* Avoids reposting the same alert (based on alert hash) even after bot restarts (using `posted_globaleas_alerts.txt`).
* Optional logging channel for status/success messages.

## Setup

1.  **Clone or Download:** Get the code files (`EAS.py`, `.gitignore`, `README.md`, `requirements.txt`).
2.  **Navigate to Folder:** Open your terminal/command prompt in the project folder.
3.  **Create & Activate Virtual Environment:**
    ```bash
    # Create the environment (use python3 if needed)
    python -m venv venv
    # Activate it:
    # Windows: venv\Scripts\activate
    # macOS/Linux: source venv/bin/activate
    ```
4.  **Install Dependencies:**
    ```bash
    pip install -r requirements.txt
    ```
5.  **Set Environment Variables:**
    Set the following environment variables using your preferred method (e.g., `export` for the current session on macOS/Linux, or system settings).
    * `DISCORD_BOT_TOKEN`: **(Required)** Your Discord bot token (keep this secret!).
    * `ALERT_CHANNEL_ID`: **(Required)** The numerical ID of the Discord channel where alerts should be posted.
    * `LOG_CHANNEL_ID` (Optional): The ID of a channel for status/success logs.
    * `CHECK_INTERVAL_SECONDS` (Optional): How often to check the API in seconds (default is 120).

## Running the Bot

Make sure your virtual environment is activated and the required environment variables are set, then run:

```bash
python EAS.py
