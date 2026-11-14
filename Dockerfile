FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# xvfb/x11vnc нужны только сервису login, но образ общий: профиль Chromium
# должен создаваться в той же Linux-среде, где его потом читает приложение.
RUN apt-get update \
    && apt-get install -y --no-install-recommends xvfb x11vnc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY app ./app

CMD ["python", "-m", "app.main"]
