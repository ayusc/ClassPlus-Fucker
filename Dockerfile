# Use official Python 3.10 slim image
FROM python:3.10-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install system dependencies (ffmpeg for processing, curl for healthcheck, wget/tar for the downloader)
RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    wget \
    tar \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Download and install the specific v0.5.1-beta of N_m3u8DL-RE
RUN wget https://github.com/nilaoda/N_m3u8DL-RE/releases/download/v0.5.1-beta/N_m3u8DL-RE_v0.5.1-beta_linux-x64_20251029.tar.gz -O downloader.tar.gz \
    && tar -xvf downloader.tar.gz \
    && mv N_m3u8DL-RE_v0.5.1-beta_linux-x64*/N_m3u8DL-RE /usr/local/bin/ \
    && chmod +x /usr/local/bin/N_m3u8DL-RE \
    && rm -rf downloader.tar.gz N_m3u8DL-RE_v0.5.1-beta_linux-x64*

# Set working directory
WORKDIR /app

# Copy requirements.txt and install Python dependencies
COPY requirements.txt .

RUN pip install --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

# Copy the entire app
COPY . .

# Update healthcheck to match FastAPI port and path
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
  CMD curl --fail http://localhost:8000/health || exit 1

EXPOSE 8000

# Start the FastAPI server (which also boots the Telegram bot via threading)
CMD ["uvicorn", "pw:app", "--host", "0.0.0.0", "--port", "8000"]
