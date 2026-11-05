FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core fontconfig curl \
    && rm -rf /var/lib/apt/lists/*
RUN mkdir -p /usr/share/fonts/truetype/montserrat && for w in Black ExtraBold; do \
    curl -fsSL -o /usr/share/fonts/truetype/montserrat/Montserrat-$w.ttf \
    "https://github.com/JulietaUla/Montserrat/raw/master/fonts/ttf/Montserrat-$w.ttf"; done && fc-cache -f
WORKDIR /bot
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip uninstall -y opencv-python opencv-contrib-python opencv-python-headless \
    && pip install --no-cache-dir "opencv-python-headless>=4.9,<5"
COPY app ./app
ENV DATA_DIR=/data PYTHONUNBUFFERED=1
CMD ["python", "-m", "app.bot"]
