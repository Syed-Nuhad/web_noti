# Dockerfile — development-friendly
FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# system deps useful for many Python packages (lxml, etc.)
RUN apt-get update \
  && apt-get install -y --no-install-recommends build-essential gcc libxml2-dev libxslt1-dev libffi-dev curl \
  && rm -rf /var/lib/apt/lists/*

# copy requirements first for caching
COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip && pip install -r /app/requirements.txt

# copy project
COPY . /app

# expose django dev port
EXPOSE 8000

# default (compose overrides for migrate+runserver)
CMD ["sh", "-c", "python manage.py runserver 0.0.0.0:8000"]