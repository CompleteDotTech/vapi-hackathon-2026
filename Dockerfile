FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements-cloud.txt ./
RUN pip install --no-cache-dir -r requirements-cloud.txt
COPY cloud_api.py cloud_worker.py ./
COPY LICENSE THIRD_PARTY_NOTICES.md ./
COPY vendor/skyscanner/LICENSE.md ./vendor/skyscanner/LICENSE.md
COPY vendor/skyscanner/skyscanner ./vendor/skyscanner/skyscanner
RUN useradd --uid 10001 --create-home app
USER app
EXPOSE 8080
CMD ["sh", "-c", "exec uvicorn cloud_api:app --host 0.0.0.0 --port ${PORT:-8080} --no-access-log"]
