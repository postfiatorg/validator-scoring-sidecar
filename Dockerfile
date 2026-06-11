# Source of the postfiatd `validator-keys` signing tool for the participation
# image. Must be a published agtipft/postfiatd tag (or digest, for reproducible
# provenance); the size suffix only selects bundled node config, the tool is the
# same. Only the `participate` target pulls it.
ARG VALIDATOR_KEYS_IMAGE=agtipft/postfiatd:testnet-light-latest

FROM python:3.11-slim AS builder

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN pip install --no-cache-dir --prefix=/install .


# Pulled only by `participate` builds: BuildKit (the default builder) skips
# stages outside the target's dependency graph; the legacy builder would not.
FROM ${VALIDATOR_KEYS_IMAGE} AS validator-keys-src


# Participation image (opt-in, built by docker-compose.participate.yml). Ubuntu
# matches the base postfiatd builds `validator-keys` on; the Debian-based slim
# image carries an older glibc that cannot load the binary. amd64-only, like
# the postfiatd image it sources the tool from.
FROM ubuntu:24.04 AS participate

ARG VALIDATOR_KEYS_BIN=/usr/local/bin/validator-keys

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# The modal extra lets the participate loop deploy the manifest-pinned Modal
# endpoint itself; local-runtime operators simply leave the credentials unset.
COPY --from=builder /build /build
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir "/build[modal]" \
    && rm -rf /build

COPY --from=validator-keys-src ${VALIDATOR_KEYS_BIN} /usr/local/bin/validator-keys
# Executability gate: an ABI-incompatible or missing signing tool must fail the
# image build, never a live round.
RUN chmod 0755 /usr/local/bin/validator-keys \
    && validator-keys --version

# Replace Ubuntu's default UID-1000 user so `sidecar` keeps the same UID as in
# the verify-only image; the shared data volume must stay writable when an
# operator switches between the two.
RUN userdel --remove ubuntu \
    && useradd --uid 1000 --create-home --shell /bin/sh sidecar \
    && mkdir -p /data \
    && chown sidecar:sidecar /data

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

USER sidecar
WORKDIR /home/sidecar

ENV PATH="/opt/venv/bin:$PATH" \
    POSTFIAT_SIDECAR_DATA_DIR=/data

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]


# Verify-only image. Kept as the final stage so a bare `docker build .` and the
# base compose file produce it; participation is an explicit opt-in target.
FROM python:3.11-slim AS runtime

RUN useradd --uid 1000 --create-home --shell /bin/sh sidecar \
    && mkdir -p /data \
    && chown sidecar:sidecar /data

COPY --from=builder /install /usr/local
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

USER sidecar
WORKDIR /home/sidecar

ENV POSTFIAT_SIDECAR_DATA_DIR=/data

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
