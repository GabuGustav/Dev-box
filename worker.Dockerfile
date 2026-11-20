FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /opt/devbox
RUN pip install --no-cache-dir "fastapi>=0.115,<1.0" "uvicorn[standard]>=0.30,<1.0" "docker>=7.0,<8.0"
COPY worker ./worker
COPY workspace-image ./workspace-image
ENV DEVBOX_WORKSPACE_IMAGE_CONTEXT=/opt/devbox/workspace-image
EXPOSE 9000
CMD ["uvicorn", "worker.main:app", "--host", "0.0.0.0", "--port", "9000"]
