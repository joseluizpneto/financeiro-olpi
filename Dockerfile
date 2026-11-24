FROM python:3.12-slim

ARG APP_GIT_COMMIT=""
ARG APP_GIT_BRANCH="main"
ARG APP_GIT_DATE=""

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=America/Araguaina \
    APP_GIT_COMMIT=${APP_GIT_COMMIT} \
    APP_GIT_BRANCH=${APP_GIT_BRANCH} \
    APP_GIT_DATE=${APP_GIT_DATE}

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 app \
    && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=app:app . .
RUN mkdir -p /app/media /app/data /app/atualizacao \
    && chmod 755 /app/docker/entrypoint.sh \
    && DEBUG=1 python manage.py collectstatic --noinput \
    && chown -R app:app /app /app/data /app/atualizacao \
    && chmod 750 /app/data /app/media \
    && chmod 775 /app/atualizacao

USER app
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/', timeout=4).status == 200 else 1)"

ENTRYPOINT ["docker/entrypoint.sh"]
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "4", "--max-requests", "500", "--max-requests-jitter", "50", "--access-logfile", "-"]
