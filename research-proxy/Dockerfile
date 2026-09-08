# 构建在 GitHub Actions 上跑（runner 到不了 jcr mirror），所以 FROM 直连 docker.io。
# 本地手动构建请先 docker pull jcr.savorcare.com/docker/library/python:3.12-slim
# 再 docker tag 成 python:3.12-slim。
FROM python:3.12-slim

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

USER nobody
EXPOSE 3100
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3100"]
