FROM python:3.11.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HF_HOME=/models/huggingface
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --uid 10001 --create-home app \
    && mkdir -p /models && chown app:app /models
COPY --chown=app:app app ./app
COPY --chown=app:app migrations ./migrations
COPY --chown=app:app tests ./tests
USER app
EXPOSE 8000
CMD ["sh", "-c", "python -m app.store && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --ws-max-size 8192 --ws-max-queue 4"]
