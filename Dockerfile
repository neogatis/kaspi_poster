FROM mcr.microsoft.com/playwright/python:v1.62.0-noble

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY . /app
RUN mkdir -p /app/data
CMD ["python", "main.py"]
