FROM python:3.12-slim

# poppler-utils gives us the real pdftotext binary every parser was
# validated against - this is the whole reason for a Docker-based host
# instead of Netlify Functions (which can't run Python at all, and
# wouldn't have poppler even if they could).
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ ./app/

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
