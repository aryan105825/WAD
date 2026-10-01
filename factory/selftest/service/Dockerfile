# @exports: container image serving app.py on port 8080
# @imports: app.py
# @env: MODE
# @env: CAPACITY
# @schema: build context is factory/selftest/service; health endpoint GET /health on port 8080
FROM python:3.12.7-slim
WORKDIR /srv
COPY app.py /srv/app.py
EXPOSE 8080
USER nobody
CMD ["python3", "-u", "/srv/app.py"]
