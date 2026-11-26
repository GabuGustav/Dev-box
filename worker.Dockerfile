FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp
WORKDIR /opt/devbox
RUN pip install --no-cache-dir "fastapi>=0.115,<1.0" "uvicorn[standard]>=0.30,<1.0" "httpx>=0.27,<1.0" "cryptography>=43,<47"
COPY worker ./worker
USER 10002:10001
EXPOSE 9000
CMD ["uvicorn", "worker.main:app", "--host", "0.0.0.0", "--port", "9000"]
