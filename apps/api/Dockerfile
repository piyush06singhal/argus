FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
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
