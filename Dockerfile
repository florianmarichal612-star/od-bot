FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080 \
    CHECKLIST_DB_PATH=/data/checklist.db

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

RUN mkdir -p /data

EXPOSE 8080

CMD ["python3", "bot.py"]
