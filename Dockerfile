FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        fonts-dejavu-core \
        libcairo2 \
        libffi8 \
        libfreetype6 \
        libgdk-pixbuf-2.0-0 \
        libharfbuzz0b \
        libjpeg62-turbo \
        libopenjp2-7 \
        libpango-1.0-0 \
        libpangoft2-1.0-0 \
        shared-mime-info \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

RUN groupadd --system app \
    && useradd --system --gid app --home-dir /app --create-home app

COPY . .
RUN chmod +x scripts/entrypoint.sh \
    && SCOPEQUOTE_ENV=development DEBUG=false python manage.py collectstatic --noinput \
    && mkdir -p /app/media /app/staticfiles \
    && chown -R app:app /app

USER app

EXPOSE 8000

ENTRYPOINT ["/app/scripts/entrypoint.sh"]
CMD ["gunicorn", "scopequote.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--error-logfile", "-"]
