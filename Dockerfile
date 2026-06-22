FROM node:22-alpine AS frontend-assets

WORKDIR /assets
COPY package.json ./
RUN npm install --omit=dev

FROM python:3.12-slim AS census-data

WORKDIR /build
RUN pip install --no-cache-dir pyproj==3.7.1 pyshp==2.3.1 shapely==2.1.1
COPY scripts/build_mainz_census.py ./
RUN python build_mainz_census.py --output /output

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt requirements-dev.txt ./
ARG INSTALL_DEV=false
RUN pip install --no-cache-dir -r requirements.txt \
    && if [ "$INSTALL_DEV" = "true" ]; then pip install --no-cache-dir -r requirements-dev.txt; fi

COPY app ./app
COPY tests ./tests
COPY pytest.ini ./
COPY --from=frontend-assets /assets/node_modules/leaflet/dist ./app/static/vendor/leaflet
COPY --from=census-data /output ./app/data/census

RUN mkdir -p /app/data/cache

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
