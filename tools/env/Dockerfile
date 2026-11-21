# Local tooling image: the labelling environment (same base + requirements.lock)
# used to build the coverage map and to label self-generated validation commits.
# Not used by the submission. Build native (arm64 on a Mac) for speed.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
RUN apt-get update && apt-get install -y --no-install-recommends git patch && rm -rf /var/lib/apt/lists/*
COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir -r /tmp/requirements.lock
ENV SKIP_INTEGRATION=0 PYTHONDONTWRITEBYTECODE=1
WORKDIR /work
