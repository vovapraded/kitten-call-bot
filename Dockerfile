FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DB_PATH=/app/data/bot.sqlite3 \
    KITTEN_DIR=/app/assets/kittens

WORKDIR /app

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --no-create-home app \
    && mkdir -p /app/data \
    && chown app:app /app/data

COPY requirements.txt ./
RUN pip install --no-cache-dir --disable-pip-version-check -r requirements.txt

COPY kitten_bot/ ./kitten_bot/
COPY assets/kittens/ ./assets/kittens/

USER 10001:10001

CMD ["python", "-m", "kitten_bot"]
