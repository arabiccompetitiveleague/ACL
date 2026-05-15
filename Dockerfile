FROM python:3.10-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    tesseract-ocr \
    tesseract-ocr-eng \
    libtesseract-dev \
    libleptonica-dev \
    pkg-config \
 [span_0](start_span)&& rm -rf /var/lib/apt/lists/*[span_0](end_span)

WORKDIR /app

# Install requirements
COPY requirements.txt .
[span_1](start_span)RUN pip install --no-cache-dir -r requirements.txt[span_1](end_span)

# Copy all files from GitHub root to /app
COPY . .

# Ensure start.sh is executable (just in case)
RUN chmod +x start.sh

# Use -u for unbuffered logs so you can see errors in Render
CMD ["python3", "-u", "main.py"]
 
