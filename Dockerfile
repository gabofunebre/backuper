FROM python:3.11-slim

# Install rclone binary for remote storage operations
RUN apt-get update \
    && apt-get install -y --no-install-recommends rclone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY orchestrator ./orchestrator

EXPOSE 5550

# Run the orchestrator using the package's __main__ entry point
CMD ["python", "-m", "orchestrator.app"]
