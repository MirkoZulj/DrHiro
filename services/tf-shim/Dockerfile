FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir httpx uvicorn starlette "redis>=5.0"
COPY shim.py /app/shim.py
EXPOSE 3200
CMD ["python", "/app/shim.py"]
