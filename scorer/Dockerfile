# Local CPU phoneme service (scorer/local.py). The Modal GPU service builds its own image from scorer.py.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/cache

# Decoding WebM/Opus and MP4/AAC needs ffmpeg, not its desktop recommendations.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements-local.txt .
RUN pip install -r requirements-local.txt

COPY engine.py local.py ./

# The model cache is a volume, so the ~1.2 GB of weights downloads once, not on every start.
RUN useradd --create-home scorer && mkdir -p /cache && chown scorer /cache
USER scorer

EXPOSE 8000
CMD ["uvicorn", "local:app", "--host", "0.0.0.0", "--port", "8000"]
