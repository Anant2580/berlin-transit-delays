FROM python:3.12-slim

WORKDIR /app

# Install Python dependencies first so Docker can cache this layer
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY collector.py stations.txt ./

# -u = unbuffered output, so log lines show up immediately in `docker compose logs`
CMD ["python", "-u", "collector.py"]
