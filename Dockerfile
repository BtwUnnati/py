# Use an official Python runtime as a parent image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Copy files
COPY bot.py /app/bot.py
COPY requirements.txt /app/requirements.txt

# Install dependencies
RUN pip install --no-cache-dir -r /app/requirements.txt

# Expose nothing (bot uses polling)
ENV PYTHONUNBUFFERED=1

# Run the bot
CMD ["python", "bot.py"]
