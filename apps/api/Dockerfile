FROM python:3.12-slim

WORKDIR /app

# Install system dependencies.
#
# ``git`` is a runtime dependency, not a build convenience: the Phase 6
# repository provider shells out to it for HEAD resolution, history, diff and
# blame. Without it inside the image, code intelligence silently degrades to
# "history unavailable" on every Docker deployment.
RUN apt-get update && apt-get install -y \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Make the app package importable for the console-script entrypoint
# (`alembic` runs from site-packages/bin, which does not include /app).
ENV PYTHONPATH=/app

# Container entrypoint runs alembic + seed before starting uvicorn
COPY docker-entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Expose port
EXPOSE 8000

# Run the application
CMD ["/entrypoint.sh"]
