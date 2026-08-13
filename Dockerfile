# 로컬 Docker Desktop 없이 GitHub Actions에서 빌드 → ECR → ECS
FROM node:20-alpine AS frontend
WORKDIR /fe
ARG VITE_RUNTIME_LABEL=""
ENV VITE_RUNTIME_LABEL=$VITE_RUNTIME_LABEL
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.11-slim
WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY backend/requirements.txt backend/requirements-postgres.txt ./
RUN pip install --no-cache-dir -r requirements-postgres.txt

COPY backend/app ./app
COPY --from=frontend /fe/dist ./static

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
