FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /opt/devbox
RUN apt-get update && apt-get install -y --no-install-recommends xfsprogs util-linux \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir "fastapi>=0.115,<1.0" "uvicorn[standard]>=0.30,<1.0" "docker>=7.0,<8.0"
COPY broker ./broker
COPY worker ./worker
COPY workspace-image ./workspace-image
ENV DEVBOX_WORKSPACE_IMAGE_CONTEXT=/opt/devbox/workspace-image
EXPOSE 9100
CMD ["uvicorn", "broker.main:app", "--host", "0.0.0.0", "--port", "9100"]
