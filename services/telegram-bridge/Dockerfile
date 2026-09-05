FROM python:3.12-slim

WORKDIR /srv
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/srv/src

COPY requirements.txt ./
COPY src ./src
RUN pip install --no-cache-dir -r requirements.txt

ENTRYPOINT ["python", "-m", "drhiro_bridge.main"]
