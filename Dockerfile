FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
# Skip the heavy browser unless you need engine "browser"/auto-fallback.
RUN grep -v '^playwright' requirements.txt > req.txt && \
    pip install --no-cache-dir -r req.txt

COPY *.py ./
COPY sites ./sites

# Default: serve the dashboard. Override for the runner via compose `runner`.
CMD ["uvicorn", "dashboard:app", "--host", "0.0.0.0", "--port", "8050"]
