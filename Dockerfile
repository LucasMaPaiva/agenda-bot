FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN groupadd --gid 1000 bot \
    && useradd --uid 1000 --gid bot --create-home bot

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=bot:bot bot.py ./
RUN mkdir -p /app/data && chown bot:bot /app/data

USER bot

CMD ["python", "bot.py"]
