# Dockerfile
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy your server code
COPY server.py .

# IMPORTANT: The server must run in stdio mode, so no exposed ports are needed.
# We use CMD to define the default execution.
CMD ["python", "server.py"]