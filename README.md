# Transponster

A Slack bot that turns audio or video into transcribed text file using ElevenLabs API.

---

## Slack Setup

1. **Create a Slack App:**
   - Go to [Slack API](https://api.slack.com/apps) and create a new app.
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

   - Install the app to your workspace to obtain the **Bot User OAuth Token** (these start with `xoxb-`).
   - In the **Basic Information** section, note your **Signing Secret**.

3. **Event Subscriptions:**
   - Enable **Event Subscriptions**.
   - Set the Request URL to your public endpoint (e.g., `https://your-domain.com/slack/events`).
   - Subscribe to the `file_shared` event.
   - Save your changes.

4. **Environment Variables:**
   - Create a `.env` file in your project root with the following (replace placeholders with your values):

     ```
     SLACK_BOT_TOKEN=xoxb-your-slack-bot-token
     SLACK_SIGNING_SECRET=your-slack-signing-secret
     ```

---

## ElevenLabs Setup

1. **Sign Up & API Key:**
   - Visit [ElevenLabs](https://elevenlabs.io) and create an account.
   - Generate your API key from your dashboard.

2. **Environment Variables:**
   - In your `.env` file, add:

     ```
     ELEVENLABS_API_KEY=your-elevenlabs-api-key
     ```

---

## Bot Setup

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
     - `SLACK_BOT_TOKEN`
     - `SLACK_SIGNING_SECRET`
     - `ELEVENLABS_API_KEY`
     - Optionally, `METRICS_FILE` (defaults to `transcription_metrics.json`)

4. **Run the Bot:**

   ```
   python bot.py
   ```

   The bot should start, and you'll see logs both in your terminal and in `bot.log`.

---

## Local Development

When developing locally, it's often necessary to expose your local server to the internet so that Slack can send events to your bot.

1. **Use Ngrok:**
   - Install [ngrok](https://ngrok.com/) if you haven't already.
   - Run your Flask server on port 3000 and then in a separate terminal run:
   
     ```
     ngrok http 3000
     ```
     
   - Ngrok will provide you with a public URL (e.g., `https://xxxxxxxx.ngrok.io`).

2. **Configure Slack Event Subscriptions:**
   - In Slack's Event Subscriptions settings, set the Request URL to:
     
     ```
     https://xxxxxxxx.ngrok.io/slack/events
     ```
     
   - Replace `xxxxxxxx` with your actual ngrok subdomain.
   
This setup allows you to test your bot locally while Slack sends events to your exposed endpoint.

---

## Deployment

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
```

3. **Run the Container**
Map port 3000 from the container to the host, load your .env, and ensure the container restarts automatically:
```
docker run -d \
  --name transponster-container \
  --env-file .env \
  -p 3000:3000 \
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
.