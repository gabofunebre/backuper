FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY orchestrator ./orchestrator

EXPOSE 5550

# Run the orchestrator using the package's __main__ entry point
CMD ["python", "-m", "orchestrator.app"]
