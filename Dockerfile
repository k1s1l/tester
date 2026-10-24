FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt requirements-dev.txt ./
ARG INSTALL_DEV=false
RUN if [ "$INSTALL_DEV" = "true" ]; then pip install -r requirements-dev.txt; else pip install -r requirements.txt; fi

COPY . .

# The repository may be uploaded through the GitHub web page as flat files
# (app--handlers--order.py). Put them back into folders (no-op if folders already exist).
RUN python unflatten.py

# Run as an unprivileged user
RUN useradd --create-home --uid 10001 bot && chown -R bot /app
USER bot

# Applies migrations (safe to run concurrently thanks to an advisory lock), then starts the bot.
CMD ["sh", "scripts/start.sh"]
