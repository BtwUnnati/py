# Use a Debian-based slim Python image
FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive

# Install runtimes and build tools for Option A:
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    g++ \
    make \
    ca-certificates \
    curl \
    wget \
    unzip \
    default-jdk \
    php-cli \
    nodejs \
    npm \
    git \
 && rm -rf /var/lib/apt/lists/*

# Create app directory
WORKDIR /app

# Copy requirements and bot code
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy bot source
COPY bot.py /app/bot.py

# Create non-root user (best practice)
RUN useradd -m runner && chown -R runner:runner /app
USER runner

# Environment variables (set on Railway dashboard)
ENV TELEGRAM_TOKEN=""
ENV OWNER_ID=""
ENV CHANNEL_LINK=""
ENV PUBLIC_MODE="on"   # default ON as requested (change to "off" to restrict to owner)

# Start bot
CMD ["python", "bot.py"]
