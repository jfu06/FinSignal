# Hugging Face Spaces (Docker SDK) — runs the Streamlit UI on port 7860.
FROM python:3.11-slim

WORKDIR /app

# Install deps first for layer caching. The root requirements.txt pulls in
# backend/requirements.txt and prefers CPU torch wheels (Spaces is CPU-only).
COPY requirements.txt ./requirements.txt
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Runtime state (jsonl logs, downloaded filings, model cache) lives on the
# ephemeral container disk; durable data (chunks/claims) lives in Neon.
ENV HF_HOME=/app/.cache PYTHONUNBUFFERED=1

EXPOSE 7860
CMD ["streamlit", "run", "backend/ui/app.py", \
     "--server.port=7860", "--server.address=0.0.0.0", "--server.headless=true"]
