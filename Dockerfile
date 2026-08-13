FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FORMULA_OCR_PRELOAD=0 \
    FORMULA_OCR_DEVICE=cpu \
    FORMULA_OCR_MODEL=/opt/formula-model \
    FORMULA_DETECTOR_MODEL=/opt/formula-detector/pix2text-mfd-1.5.onnx \
    HF_HOME=/home/appuser/.cache/huggingface \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt constraints.txt ./
RUN curl --fail --location --retry 5 --retry-all-errors --progress-bar \
      -o "/tmp/torch-2.7.1+cpu-cp311-cp311-manylinux_2_28_x86_64.whl" \
      "https://download.pytorch.org/whl/cpu/torch-2.7.1%2Bcpu-cp311-cp311-manylinux_2_28_x86_64.whl" \
    && pip install --no-cache-dir \
      "/tmp/torch-2.7.1+cpu-cp311-cp311-manylinux_2_28_x86_64.whl" \
    && rm -f /tmp/torch-*.whl \
    && pip install --no-cache-dir -c constraints.txt -r requirements.txt
RUN HF_HUB_OFFLINE=0 TRANSFORMERS_OFFLINE=0 python -c "from huggingface_hub import snapshot_download; snapshot_download(repo_id='breezedeus/pix2text-mfr-1.5', local_dir='/opt/formula-model', max_workers=4)"
RUN HF_HUB_OFFLINE=0 TRANSFORMERS_OFFLINE=0 python -c "from huggingface_hub import snapshot_download; snapshot_download(repo_id='breezedeus/pix2text-mfd-1.5', local_dir='/opt/formula-detector', max_workers=4)"
COPY app ./app

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /home/appuser/.cache/huggingface \
    && chown -R appuser:appuser /app /home/appuser/.cache /opt/formula-model /opt/formula-detector
USER appuser
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=5 \
  CMD python -c "import json,urllib.request; data=json.load(urllib.request.urlopen('http://127.0.0.1:8080/__tcb_probe__',timeout=3)); raise SystemExit(0 if data.get('ok') else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
