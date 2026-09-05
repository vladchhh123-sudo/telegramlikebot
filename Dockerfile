FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot ./bot
COPY run.py .

RUN mkdir -p /app/data

CMD ["python", "run.py"]
