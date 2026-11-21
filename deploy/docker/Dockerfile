FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash git \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home microtask \
    && install -d -o microtask -g microtask /data

ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY mt .
COPY scripts/ scripts/
COPY templates/ templates/
COPY tools/ tools/
COPY .agents/skills/ .agents/skills/
COPY examples/ examples/
COPY deploy/docker/seed.py seed.py
COPY deploy/docker/smoke.py smoke.py

USER microtask
