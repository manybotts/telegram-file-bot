FROM python:3.10-slim-bullseye

WORKDIR /app

# System dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code
COPY . .

# Runtime configuration
CMD ["uvicorn", "bot:web_app", "--host", "0.0.0.0", "--port", "8000"]
