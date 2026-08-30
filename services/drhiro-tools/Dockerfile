FROM python:3.12-slim

WORKDIR /srv
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

# MCP SSE transport port
EXPOSE 3100
# Approval-gated export directory (named volume)
VOLUME /data/exports

ENTRYPOINT ["python", "-m", "drhiro_tools.server"]
