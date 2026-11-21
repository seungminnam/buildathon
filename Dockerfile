# Blast Radius entry. The scorer builds this on linux/amd64 and runs it with
# no network. The base image is the one the commits were labelled in.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# The labelling environment: the selector runs some candidate tests itself.
COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir -r /tmp/requirements.lock && rm /tmp/requirements.lock

# sqlglot at the pinned commit: the source the patches apply to.
ARG SQLGLOT_SHA=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
RUN python -c "import io, tarfile, urllib.request; \
url = 'https://github.com/tobymao/sqlglot/archive/${SQLGLOT_SHA}.tar.gz'; \
tarfile.open(fileobj=io.BytesIO(urllib.request.urlopen(url).read())).extractall('/opt', filter='data')" \
 && mv /opt/sqlglot-${SQLGLOT_SHA} /opt/sqlglot \
 && python -m compileall -q /opt/sqlglot/sqlglot /opt/sqlglot/tests > /dev/null; \
 chmod -R a+rX /opt/sqlglot

COPY selector.py /app/selector.py
COPY blast /app/blast
COPY maps /app/maps
# Fixed string hashing: the TPC digest below must come out the same at
# build time and at run time.
ENV PYTHONHASHSEED=0 SKIP_INTEGRATION=0
# Precompute build-time maps on the target CPU: TPC query digests + per-query
# coverage, baseline import fingerprint, and calibrated per-test runtimes.
RUN cd /app \
 && python -m blast.tpcdigest /opt/sqlglot /app/maps/tpc_digest.json \
 && python -m blast.probe /opt/sqlglot /app/maps/probe_base.json \
 && python -m blast.bench /opt/sqlglot /app/maps/coverage.json /app/maps/actual_ms.json \
 && python -m compileall -q /app && chmod -R a+rX /app

ENV PYTHONDONTWRITEBYTECODE=1
CMD ["python", "/app/selector.py"]
