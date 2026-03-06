FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

ENV PYTHONUNBUFFERED=1
ENV PORT=8000

EXPOSE 8000
CMD ["sh", "-c", "python3 rag-chatbot.py serve 8 ${PORT:-8000}"]
