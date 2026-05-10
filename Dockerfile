FROM python:3.11-slim

WORKDIR /app

# System deps for faiss-cpu and sentence-transformers
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Data directory for SQLite and FAISS index
RUN mkdir -p data

# Run as non-root
RUN useradd -m -u 1000 workflow && chown -R workflow:workflow /app
USER workflow

EXPOSE 8000

CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
