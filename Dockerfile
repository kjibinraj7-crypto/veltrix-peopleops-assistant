FROM python:3.11-slim

# No system packages required: OCR and PDF rasterisation are pure-Python / pip
# (PyMuPDF renders pages; RapidOCR runs local ONNX models). This keeps the
# image self-contained with zero external binaries or manual install steps.

WORKDIR /app

# ─── Python dependencies ──────────────────────────────────────────────────────
COPY requirements.txt .

# Install CPU-only PyTorch first. The default PyPI `torch` wheel bundles ~2 GB of
# CUDA libraries that this CPU-only service never uses; the CPU build keeps the
# image small and the `docker compose up` build fast (health within 10 minutes).
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

RUN pip install --no-cache-dir -r requirements.txt

# RapidOCR depends on `opencv-python`, whose GUI wheels link against X/GL system
# libraries (libxcb/libGL) that `python:3.11-slim` does not ship. The headless
# build is API-compatible for RapidOCR (it never opens a window) and needs no
# system packages, so swap it in to keep the image free of apt/system binaries.
RUN pip uninstall -y opencv-python && pip install --no-cache-dir opencv-python-headless

# ─── Pre-download models at build time ────────────────────────────────────────
# Caching both models in the image avoids any runtime download during
# ingestion, ensuring /health returns 200 well within the 10-minute deadline.
RUN python -c "\
from sentence_transformers import SentenceTransformer; \
SentenceTransformer('BAAI/bge-small-en-v1.5'); \
print('BGE model downloaded successfully')"

RUN python -c "\
from rapidocr_onnxruntime import RapidOCR; \
RapidOCR(); \
print('RapidOCR models downloaded successfully')"

# Warm the flashrank reranker cache at the exact path the app uses
# (src/retrieval/reranker.py -> cache_dir="/tmp/flashrank"), so the first /chat
# does not have to download the model mid-request. Non-fatal: if it cannot be
# fetched the app falls back to un-reranked order and still serves.
RUN python -c "\
from flashrank import Ranker; \
Ranker(model_name='ms-marco-MiniLM-L-12-v2', cache_dir='/tmp/flashrank'); \
print('Reranker model cached')" \
    || echo 'WARN: flashrank warm-up skipped; reranker will fall back at runtime'

# ─── Application source ───────────────────────────────────────────────────────
COPY src/ ./src/

# ─── Startup ──────────────────────────────────────────────────────────────────
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
