FROM jcr.savorcare.com/docker/library/python:3.12-slim

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

USER nobody
EXPOSE 3100
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3100"]
