FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# cache yolo weights in image layer
RUN python -c "from ultralytics import YOLO; YOLO('yolov8n.pt')"

COPY server.py detector.py storage.py state_machine.py notifier.py config.py ./
COPY static/ ./static/

RUN mkdir -p /app/data /app/data/images /app/data/videos

EXPOSE 8000
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
