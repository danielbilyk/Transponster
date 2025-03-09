# Use an official Python runtime as a parent image
FROM python:3.12-slim

# Create a directory for the app
WORKDIR /app

# Copy the rest of your bot’s code
COPY . /app

# Copy the entrypoint script and make it executable
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Copy the requirements and install them
COPY requirements.txt /app/
RUN pip3 install --upgrade pip
RUN pip3 install --no-cache-dir -r requirements.txt
RUN pip3 install gunicorn
RUN sh /app/entrypoint.sh

# Expose the port your Flask app runs on (3000 by default)
EXPOSE 3000

# Set environment variables for Python
ENV PYTHONUNBUFFERED=1

# Start the bot
CMD ["gunicorn", "-b", "0.0.0.0:3000", "bot:flask_app"]
.