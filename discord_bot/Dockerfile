FROM python:3.11-slim

RUN groupadd -r app && useradd -r -g app -d /app -s /bin/false app

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN chown -R app:app /app

USER app

CMD ["python", "bot.py"]
