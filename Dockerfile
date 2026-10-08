# Relay only. EasyAgent at home dials out to this container.
# There is no data directory and no volume for transcripts.
FROM python:3.12-slim

WORKDIR /opt/easyagent-relay

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY easyagent ./easyagent

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=44731

EXPOSE 44731

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','44731')+'/healthz')"]

CMD ["python", "-m", "easyagent.relay"]
