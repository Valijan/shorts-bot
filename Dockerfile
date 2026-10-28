FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core fontconfig \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /bot
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
ENV DATA_DIR=/data PYTHONUNBUFFERED=1
CMD ["python", "-m", "app.bot"]
