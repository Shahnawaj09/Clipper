# Use official Python runtime
FROM python:3.11-slim

# Install system dependencies (ffmpeg for yt-dlp)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg wget curl && \
    rm -rf /var/lib/apt/lists/*

# Set work directory
WORKDIR /app

# Copy dependency files first
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your bot code
COPY . .

# Expose port (not really used for bots, but Render requires it)
EXPOSE 8080

# Start your bot
CMD ["python", "clipper_bot.py"]
