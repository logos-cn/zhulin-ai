FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    APP_PORT=199

WORKDIR /app

RUN useradd --create-home --shell /usr/sbin/nologin appuser

COPY requirements.txt /app/requirements.txt
RUN python -m pip install --upgrade pip \
    && python -m pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

RUN mkdir -p /app/data /app/logs \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 199

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "199"]
