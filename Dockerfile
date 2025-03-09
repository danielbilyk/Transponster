# Use an official Python runtime as a parent image
FROM python:3.12-slim

# Create a directory for the app
WORKDIR /app

# Copy the requirements and install them
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your bot’s code
COPY . /app

# Expose the port your Flask app runs on (3000 by default)
EXPOSE 3000

# Set environment variables for Python
ENV PYTHONUNBUFFERED=1

# Start the bot
CMD ["python", "bot.py"]
