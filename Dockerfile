# Use an official Python runtime as a parent image
FROM python:3.12-slim

# Create a non-root user
RUN groupadd --system app && useradd --system --gid app app

# Create a directory for the app
WORKDIR /app

# Copy requirements and install dependencies as root
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

# Switch to non-root user for security
USER app

# Copy the rest of your bot's code
COPY --chown=app:app . .

# Expose the port your app runs on
EXPOSE 3000

# Set environment variables for Python
ENV PYTHONUNBUFFERED=1

# Start the bot
CMD ["uvicorn", "bot:api", "--host", "0.0.0.0", "--port", "3000"]