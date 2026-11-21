FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates coreutils \
    && git --version && timeout --version \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin devbox \
    && mkdir -p /workspace \
    && chown 10001:10001 /workspace
USER 10001:10001
WORKDIR /workspace
CMD ["sleep", "infinity"]
