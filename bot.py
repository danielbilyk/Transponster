import logger_setup
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
import logging
import os
from contextlib import asynccontextmanager

# Import the async app object and the correct handler for FastAPI
from slack_events import app 
from slack_bolt.adapter.fastapi.async_handler import AsyncSlackRequestHandler

# --- Lifespan Event Handler ---
@asynccontextmanager
async def lifespan(api_app: FastAPI):
    # This block runs on startup
    logging.info("Application startup...")
    SLACK_STARTUP_CHANNEL = os.getenv("SLACK_STARTUP_CHANNEL")
    if SLACK_STARTUP_CHANNEL:
        try:
            await app.client.chat_postMessage(
                channel=SLACK_STARTUP_CHANNEL,
                text=":rocket: Я запустився і готовий робить штук."
            )
            logging.info(f"Startup message sent to {SLACK_STARTUP_CHANNEL}")
        except Exception as e:
            logging.error(f"Failed to send startup message: {e}")
    else:
        logging.warning("SLACK_STARTUP_CHANNEL not set; skipping startup ping.")
    
    yield # The application runs while the lifespan manager is in this 'yield' state

    # This block runs on shutdown
    logging.info("Application shutdown...")


# --- FastAPI Setup ---
api = FastAPI(lifespan=lifespan)
handler = AsyncSlackRequestHandler(app)

@api.get("/")
async def github_redirect():
    logging.info("Redirecting to GitHub")
    return RedirectResponse(url="https://github.com/danielbilyk/Transponster")

@api.post("/slack/events")
async def slack_events_endpoint(req: Request):
    return await handler.handle(req)
