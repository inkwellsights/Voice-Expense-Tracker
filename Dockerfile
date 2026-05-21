FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Dhaka

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot ./bot

# Run as a non-root user; the bot only needs outbound network.
RUN useradd --create-home --shell /bin/bash bot && chown -R bot:bot /app
USER bot

CMD ["python", "-m", "bot.main"]
