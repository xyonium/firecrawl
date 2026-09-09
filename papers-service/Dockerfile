# 构建在 GitHub Actions 上跑（runner 到不了 jcr mirror），所以 FROM 直连 docker.io。
# 本地手动构建请先 docker pull jcr.savorcare.com/docker/library/python:3.12-slim
# 再 docker tag 成 python:3.12-slim。
FROM python:3.12-slim

# tool.py 来自 paper-search 仓库（xyonium/paper-search）。工作流构建时把
# PAPER_SEARCH_REF 解析成 main 的 commit sha 传入——ref 变化会让 ADD 层缓存
# 自然失效；本地手动构建默认 main（注意 ADD 远程文件按 URL 字符串缓存，
# 同一个 ref 名不会自动拉新，所以正式构建一定要传 sha）。
ARG PAPER_SEARCH_REF=main

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
ADD https://raw.githubusercontent.com/xyonium/paper-search/${PAPER_SEARCH_REF}/tool.py /srv/vendor/tool.py
COPY app ./app

USER nobody
EXPOSE 3200
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3200"]
