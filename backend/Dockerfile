FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY requirements.txt requirements-postgres.txt ./
RUN pip install --no-cache-dir -r requirements-postgres.txt
RUN python -c "from openai_codex.client import CodexConfig, _resolve_codex_bin; import subprocess; subprocess.run([str(_resolve_codex_bin(CodexConfig())), '--version'], check=True)"

COPY app ./app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
