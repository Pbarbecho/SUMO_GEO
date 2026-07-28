# Backend = FastAPI + SUMO (managed mode runs SUMO in this same container).
# The eclipse-sumo wheel bundles xerces/proj/FOX/GLU/ssl; only libGL is needed
# from the system. If you prefer to reuse your existing SUMO image instead,
# base this stage on it and set APP_SUMO_MODE=remote (see docker-compose.yml).
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SUMO_HOME=/usr/local/lib/python3.11/site-packages/sumo

# The eclipse-sumo wheel bundles xerces/proj/FOX/GLU/ssl, but the `sumo` binary
# still dynamically links these X11/GL/atomic system libs (see `ldd sumo`), which
# python:3.11-slim does not ship. Without them SUMO fails to start.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       libgl1 libglx-mesa0 libgomp1 libatomic1 libexpat1 \
       libx11-6 libxext6 libxrender1 libxcb1 libxau6 libxdmcp6 libbsd0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
