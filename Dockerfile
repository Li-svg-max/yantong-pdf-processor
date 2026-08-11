FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FORMULA_OCR_EAGER_LOAD=1 \
    FORMULA_OCR_DEVICE=cpu

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl libgomp1 libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt constraints.txt ./
RUN curl --fail --location --retry 5 --retry-all-errors --progress-bar \
      -o /tmp/torch.whl \
      "https://download.pytorch.org/whl/cpu/torch-2.7.1%2Bcpu-cp311-cp311-manylinux_2_28_x86_64.whl" \
    && curl --fail --location --retry 5 --retry-all-errors --progress-bar \
      -o /tmp/torchvision.whl \
      "https://download.pytorch.org/whl/cpu/torchvision-0.22.1%2Bcpu-cp311-cp311-manylinux_2_28_x86_64.whl" \
    && pip install --no-cache-dir --no-deps /tmp/torch.whl /tmp/torchvision.whl \
    && rm -f /tmp/torch.whl /tmp/torchvision.whl \
    && pip install --no-cache-dir -c constraints.txt -r requirements.txt
COPY app ./app

RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=5 \
  CMD python -c "import json,urllib.request; data=json.load(urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=3)); raise SystemExit(0 if data.get('ok') else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
