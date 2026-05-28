FROM python:3.11-slim AS builder

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN pip install --no-cache-dir --prefix=/install .


FROM python:3.11-slim

RUN useradd --create-home --shell /bin/sh sidecar \
    && mkdir -p /data \
    && chown sidecar:sidecar /data

COPY --from=builder /install /usr/local
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

USER sidecar
WORKDIR /home/sidecar

ENV POSTFIAT_SIDECAR_DATA_DIR=/data

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
