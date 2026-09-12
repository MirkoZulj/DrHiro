FROM python:3.12-slim
WORKDIR /app
RUN apt-get update -qq && apt-get install -y -qq sshpass openssh-client && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY service.py .
CMD ["python3", "-m", "uvicorn", "service:app", "--host", "0.0.0.0", "--port", "8090"]
