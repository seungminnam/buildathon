# Blast Radius entry. The scorer builds this on linux/amd64 and runs it with
# no network. The base image is the one the commits were labelled in.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# sqlglot at the pinned commit: the source the patches apply to.
ARG SQLGLOT_SHA=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
RUN python -c "import io, tarfile, urllib.request; \
url = 'https://github.com/tobymao/sqlglot/archive/${SQLGLOT_SHA}.tar.gz'; \
tarfile.open(fileobj=io.BytesIO(urllib.request.urlopen(url).read())).extractall('/opt', filter='data')" \
 && mv /opt/sqlglot-${SQLGLOT_SHA} /opt/sqlglot \
 && python -m compileall -q /opt/sqlglot/sqlglot /opt/sqlglot/tests > /dev/null; \
 chmod -R a+rX /opt/sqlglot

# The packages the commits were labelled with, so tests the selector runs
# itself behave as they did there.
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -r /app/requirements.lock

COPY selector.py /app/selector.py
COPY blast /app/blast
COPY maps /app/maps
RUN python -m compileall -q /app && chmod -R a+rX /app

ENV PYTHONDONTWRITEBYTECODE=1 SKIP_INTEGRATION=0
CMD ["python", "/app/selector.py"]
