FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

COPY pyproject.toml ./
COPY src ./src
# The scripts extra brings pypdf, so scripts/ingest_pdfs.py runs inside this image too.
RUN pip install --no-cache-dir ".[scripts]"

# Migrations and operator scripts ship in the image: `alembic upgrade head`, seed_demo.py,
# seed_students.py, ingest_pdfs.py and ops_report.py all run as one-off containers from the
# same image the API runs from (deploy/compose.prod.yml, docs/DEPLOY.md). Same code, same
# dependencies, same connection strings - no second environment to drift.
COPY alembic.ini ./
COPY alembic ./alembic
COPY scripts ./scripts

RUN useradd -m appuser
USER appuser

EXPOSE 8000
CMD ["uvicorn", "eap.main:app", "--host", "0.0.0.0", "--port", "8000"]