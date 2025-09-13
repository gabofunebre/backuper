FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY orchestrator ./orchestrator

EXPOSE 5550

CMD ["python", "-m", "orchestrator.app"]
