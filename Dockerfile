FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

ENV PYTHONUNBUFFERED=1
EXPOSE 10000

# Copy dependency definition and install requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application scripts and profile session
COPY flipkart_watch.py .
COPY login_flipkart.py .
COPY flipkart_profile ./flipkart_profile

# Run unbuffered Python script
CMD ["python", "-u", "flipkart_watch.py"]
