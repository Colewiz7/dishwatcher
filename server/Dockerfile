# Two-stage by intent, not by layer count.
#
# v1's image installed ultralytics, which drags in the full torch stack, so the
# image landed around 2-3 GB. That was already a bad trade when yolo made the
# dirty/clean decision. Now it does not: SSIM decides, and yolo only writes
# labels onto the dashboard. The k3s node has 19 GB free on its root disk, so
# paying 3 GB for cosmetic labels is not sensible.
#
# So the default build is slim and has no torch. Build with
# --build-arg WITH_YOLO=true if you want the labels back; detector.py already
# degrades gracefully when ultralytics is absent and logs that it has.

FROM python:3.11-slim AS base

ARG WITH_YOLO=false

RUN apt-get update && apt-get install -y --no-install-recommends \
      libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt requirements-yolo.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && if [ "$WITH_YOLO" = "true" ]; then \
         pip install --no-cache-dir -r requirements-yolo.txt \
         && python -c "from ultralytics import YOLO; YOLO('yolov8n.pt')"; \
       fi

COPY server.py detector.py calibration.py metrics.py storage.py \
     state_machine.py notifier.py config.py ./
COPY static/ ./static/

RUN mkdir -p /app/data /app/data/images /app/data/videos

# runs unprivileged; the k8s manifest also sets runAsNonRoot
RUN useradd -u 1000 -m app && chown -R app:app /app
USER app

EXPOSE 8000

# readyz is deliberately strict about calibration, so it is the probe target
# in the manifest rather than healthz.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status==200 else 1)"

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
