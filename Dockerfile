FROM python:3.12-slim
RUN useradd --create-home --uid 10001 comic && mkdir /data && chown comic:comic /data
WORKDIR /app
COPY requirements.lock.txt .
RUN pip install --no-cache-dir -r requirements.lock.txt
COPY comic_server comic_server
EXPOSE 8000
# The entrypoint prepares a mounted data directory, then drops to the comic user.
CMD ["python", "-m", "comic_server.container_start"]
