FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

WORKDIR /code

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Sin esto el proceso corre como root dentro del contenedor: si alguien
# encuentra una RCE en algún endpoint, hereda root en vez de un usuario sin
# privilegios.
RUN useradd --create-home --uid 1000 appuser && chown -R appuser:appuser /code
USER appuser

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
