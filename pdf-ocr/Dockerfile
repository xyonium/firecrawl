# FROM 直连 docker.io：GH Actions runner 到不了 jcr.savorcare.com mirror。
# 本地手动构建需先 `docker pull jcr.savorcare.com/docker/library/python:3.12-slim`
# 再 `docker tag` 成 python:3.12-slim。
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

USER nobody
EXPOSE 3200
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3200"]
