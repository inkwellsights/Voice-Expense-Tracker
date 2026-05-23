FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Dhaka

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

RUN useradd --create-home --shell /bin/bash proxy \
    && chown -R proxy:proxy /app
USER proxy

EXPOSE 5007

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "5007", "--log-level", "info", "--no-access-log"]
