FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY jarvis ./jarvis
COPY chat.py .
ENV DATABASE_PATH=/data/jarvis.db
VOLUME ["/data"]
EXPOSE 8000
CMD ["uvicorn", "jarvis.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
