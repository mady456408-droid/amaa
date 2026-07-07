FROM mcr.microsoft.com/playwright/python:v1.61.0-jammy
WORKDIR /app

# Arabic + fallback fonts
RUN apt-get update && apt-get install -y \
    fonts-noto-core \
    fonts-noto-extra \
    fonts-dejavu \
    fonts-freefont-ttf \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "bot.py"]
