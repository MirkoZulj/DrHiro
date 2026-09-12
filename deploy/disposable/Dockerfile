# Disposable stack image for the isolated trusted-ingress vertical slice.
#
# One image, several roles. It carries the REAL T1 API source so the trusted ingress
# persists into genuine T1 tables (via the real ORM models and the real Alembic
# migrations), rather than into a lookalike schema.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app:/app/drhiro_src \
    ALEMBIC_CONFIG=/app/alembic.ini

RUN pip install --no-cache-dir \
        "psycopg2-binary>=2.9" \
        "SQLAlchemy>=2.0" \
        "alembic>=1.13" \
        "cryptography>=42" \
        "pydantic>=2" \
        "pydantic-settings>=2"

WORKDIR /app

# Real T1 application source + migration chain.
COPY apps/api/src /app/drhiro_src
COPY apps/api/alembic /app/alembic
COPY apps/api/alembic.ini /app/alembic.ini

# Disposable-stack roles.
COPY deploy/disposable/app /app
COPY deploy/disposable/sql /app/sql

CMD ["python", "-u", "/app/ingress.py"]
