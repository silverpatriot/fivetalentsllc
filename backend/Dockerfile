FROM python:3.12-slim

# psycopg2-binary needs libpq at runtime; build-essential covers any sdists
# without wheels for this platform. Pango/Cairo/GDK-PixBuf are WeasyPrint's
# real system dependencies (Phase 7 Task 3, PDF export) — Pango, GdkPixbuf,
# and Cairo can't be installed via pip, per WeasyPrint's own install docs.
# fonts-liberation gives it real (metric-compatible Times/Arial) fonts to
# render with — otherwise a slim base image has no fonts at all and every
# PDF falls back to whatever generic default the container happens to have.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq-dev \
        build-essential \
        libcairo2 \
        libpango-1.0-0 \
        libpangocairo-1.0-0 \
        libgdk-pixbuf-2.0-0 \
        libffi-dev \
        shared-mime-info \
        fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
