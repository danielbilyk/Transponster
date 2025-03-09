import logger_setup
from flask import Flask, request, redirect
import logging
from slack_events import app as slack_app, handler

flask_app = Flask(__name__)

@flask_app.route("/", methods=["GET"])
def slack_events():
	return redirect("https://github.com/danielbilyk/Transponster")

@flask_app.route("/slack/events", methods=["POST"])
def slack_events():
	return handler.handle(request)

if __name__ == "__main__":
	logging.info("Starting Flask server on port 3000")
	flask_app.run(host="0.0.0.0", port=3000)