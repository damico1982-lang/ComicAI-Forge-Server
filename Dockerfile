FROM python:3.12-slim
RUN useradd --create-home --uid 10001 comic && mkdir /data && chown comic:comic /data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY comic_server comic_server
ENV COMIC_DATA_DIR=/data
USER comic
EXPOSE 8000
CMD ["uvicorn", "comic_server.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]
