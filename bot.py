import logger_setup
from flask import Flask, request, redirect
import logging
import os
from slack_events import app, handler

SLACK_STARTUP_CHANNEL = os.getenv("SLACK_STARTUP_CHANNEL")

flask_app = Flask(__name__)

@flask_app.route("/", methods=["GET"])
def github_redirect():
	logging.info("Redirecting to GitHub")
	return redirect("https://github.com/danielbilyk/Transponster")

@flask_app.route("/slack/events", methods=["POST"])
def slack_events():
	return handler.handle(request)

if __name__ == "__main__":
	# ——— send startup ping ———
    if SLACK_STARTUP_CHANNEL:
        try:
            app.client.chat_postMessage(
                channel=SLACK_STARTUP_CHANNEL,
                text=":rocket: Я запустився і готовий робить штук."
            )
            logging.info(f"Startup message sent to {SLACK_STARTUP_CHANNEL}")
        except Exception as e:
            logging.error(f"Failed to send startup message: {e}")
    else:
        logging.warning("SLACK_STARTUP_CHANNEL not set; skipping startup ping.")

    logging.info("Starting Flask server on port 3000")
    flask_app.run(host="0.0.0.0", port=3000)