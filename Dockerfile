# Use a stable slim Python image
FROM python:3.11-slim

# avoid interactive prompts
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1

# install system packages needed for ffmpeg, building wheels, etc.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
    ca-certificates \
    wget \
    git \
    ffmpeg \
    build-essential \
    pkg-config \
    libffi-dev \
    libssl-dev \
    gcc \
    python3-dev \
    curl \
    cargo \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

# create app dir
WORKDIR /app

# copy requirements and install (upgrade pip, setuptools, wheel first)
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --upgrade pip setuptools wheel
RUN pip install -r /app/requirements.txt

# copy bot code
COPY . /app

# create tmp dir for clips and set permissions
RUN mkdir -p /app/tmp_clips && chmod -R 777 /app/tmp_clips

# ensure the env file (optional) is not copied publicly, but if you have .env it will be included.
ENV TMP_DIR=/app/tmp_clips

# default command — run the bot
CMD ["python", "clipper_bot.py"]
