FROM python:3.12-slim

# TZ only affects log timestamps; the 09:00 trigger is pinned to Asia/Singapore in code.
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TZ=Asia/Singapore

RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
RUN useradd --create-home appuser
USER appuser

CMD ["python", "-m", "src.bot"]
