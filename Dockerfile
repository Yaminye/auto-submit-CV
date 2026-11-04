# App image: Python + dependencies only. The code is bind-mounted at /app by docker-compose,
# so edits to code/config apply on `docker compose restart` - rebuild only when requirements.txt changes.
FROM python:3.14-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
# playwright is only needed by apply.py, which runs on the Mac (it needs a visible Chrome)
RUN grep -v '^playwright' requirements.txt > /tmp/req.txt && pip install --no-cache-dir -r /tmp/req.txt
