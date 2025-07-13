# Transponster — Audio Transcriber Bot for Slack

_Because every PM needs a vibe-coding project that's not good enough for production but gets the job done nevertheless._

This is a Slack bot that turns audio or video into transcribed text file using ElevenLabs API. For all of this to work, you will need to set up [Slack](#-slack-setup), [ElevenLabs](#-elevenlabs-setup), [Google Drive](#-google-drive-integration), and then [the bot itself](#-bot-setup).

---

## 🧵 Slack Setup

1. **Create a Slack App:**
   - Go to [Slack API](https://api.slack.com/apps) and create a new app: "Create New App" → "From scratch"
   - Select the workspace where you have permissions.

2. **Configure OAuth & Permissions:**
   - In the **OAuth & Permissions** section, add the following [scopes](https://api.slack.com/scopes):

     **Bot Token Scopes**  
     - `channels:history`  
     - `chat:write`
     - `files:read`
     - `files:write`
     - `groups:history`
     - `mpim:history`
     - `users:read`

     **User Token Scopes**
     - `files:read`

   - Still under **OAuth & Permissions**, click "Install App".
   - Then, obtain the **Bot User OAuth Token** (these start with `xoxb-`).
   - Then, under **Basic Information** section, note your **Signing Secret**.

3. **Event Subscriptions:**
   - Enable **Event Subscriptions**.
   - Set the Request URL to your public endpoint (e.g., `https://your-domain.com/slack/events`).
   - Subscribe to the `file_shared` event.
   - Save your changes.

---

## 🧬 ElevenLabs Setup

1. **Sign Up & API Key:**
   - Visit [ElevenLabs](https://elevenlabs.io) and create an account.
   - Generate your API key from your dashboard.

---

## ☁️ Google Drive Integration

Transponster can automatically upload your transcripts as Word documents (.docx) to a shared Google Drive. Each user gets their own folder inside the shared drive, and every transcript is saved there for easy access and sharing.

### What the bot does with Google Drive:
- Creates a shared drive (if not already present) named `Transponster`.
- Creates a personal folder for each Slack user (by display name) inside the shared drive.
- Uploads every transcript as a `.docx` file to your folder, so you can always find your files in Drive.
- Shares a direct link to the file and your folder in Slack after processing.

### Google Cloud Setup
1. **Create a Google Cloud Project & Service Account:**
   - Go to [Google Cloud Console](https://console.cloud.google.com/).
   - Create a new project (or use an existing one).
   - Enable the **Google Drive API** for your project.
   - Create a **Service Account** with the `Editor` role (or at least permission to manage files in Drive).
   - Generate a **JSON key** for the service account and download it.

2. **Share the Shared Drive with the Service Account:**
   - In Google Drive, create a shared drive named `Transponster` (or use an existing one).
   - Share the drive with your service account's email (found in the JSON key) and give it full access.

3. **Set Up the Environment Variable:**
   - Copy the entire contents of your service account JSON key.
   - In your `.env` file, add:
     ```
     GOOGLE_CREDENTIALS_JSON='{"type": "service_account", ...}'
     ```
     (Paste the JSON as a single line, escaping quotes as needed.)

---

## 🤖 Bot Setup

1. **Clone the Repository:**

   ```
   git clone https://github.com/danielbilyk/Transponster.git
   cd Transponster
   ```

2. **Create Virtual Environment & Install Dependencies:**

   ```
   python3 -m venv venv
   source venv/bin/activate    # For Windows use: venv\Scripts\activate
   pip install -r requirements.txt
   ```

3. **Configure Environment Variables:**
   - Ensure your `.env` file includes:
   ```
   SLACK_BOT_TOKEN=xoxb-your-slack-bot-token
   SLACK_SIGNING_SECRET=your-slack-signing-secret
   ELEVENLABS_API_KEY=your-elevenlabs-api-key
   GOOGLE_CREDENTIALS_JSON='{"type": "service_account", ...}'

   # Optional: Set a channel for the bot to post a startup message
   SLACK_STARTUP_CHANNEL=C0XXXXXXX
   ```

4. **Run the Bot:**

   ```
   uvicorn bot:api --host 0.0.0.0 --port 3000
   ```

   The bot will start, and you'll see logs both in your terminal and in `bot.log`.

---

## 🌐 Local Development

To develop locally, you need to expose your local server to the internet so that Slack can send events to your bot. I suggest using Ngrok.

1. Run your bot server first:
   
   In one terminal, start the bot:
   ```
   uvicorn bot:api --host 0.0.0.0 --port 3000
   ```

2. **Use Ngrok:**
   - Install [ngrok](https://ngrok.com/) if you haven't already.
   - In a separate terminal window, run:
   
     ```
     ngrok http 3000
     ```
     
   - Ngrok will provide you with a public URL (e.g., `https://xxxxxxxx.ngrok.io`).

3. **Configure Slack Event Subscriptions:**
   - In Slack's Event Subscriptions settings, set the Request URL to:
     
     ```
     https://xxxxxxxx.ngrok.io/slack/events
     ```
     
   - Replace `xxxxxxxx` with your actual ngrok subdomain.
   
This setup allows you to test your bot locally while Slack sends events to your exposed endpoint.

---

## 🐳 Deployment

1. **Build the Docker Image**  
   
   From the project root (where `Dockerfile` is located), run:
   ```
   docker build -t transponster-bot .
   ```

2. **Create a `.env` File**
   
   Make sure you have a `.env` file containing:
   ```
   SLACK_BOT_TOKEN=xoxb-your-slack-bot-token
   SLACK_SIGNING_SECRET=your-slack-signing-secret
   ELEVENLABS_API_KEY=your-elevenlabs-api-key
   GOOGLE_CREDENTIALS_JSON='{"type": "service_account", ...}'

   # Optional: Set a channel for the bot to post a startup message
   SLACK_STARTUP_CHANNEL=yor-slack-startup-channel
   ```

3. **Run the Container**
   
   Map port 3000 from the container to the host, load your `.env`, and ensure the container restarts automatically:
   ```
   docker run -d \
     --name transponster-container \
     --env-file .env \
     -p 3000:3000 \
     -v /path/on/host/bot.log:/app/bot.log \
     --restart=always \
     transponster-bot
   ```

4. **Confirm It’s Running**
   - Check container logs:
   ```
   docker logs -f transponster-container
   ```
   - If you need a public HTTPS endpoint (for Slack), set up a reverse proxy (e.g., Apache/Nginx) pointing to port 3000.

   You can then configure Slack to send requests to your server’s address or domain, forwarding to port 3000 if needed.
   ```
   https://your-domain.tld/slack/events
   ```