FROM python:3.12-alpine
WORKDIR /app
COPY app/app.py app/dashboard.html app/icon.svg ./
COPY app/fonts ./fonts
ENV DB_PATH=/data/hub.db \
    PORT=8765
VOLUME /data
EXPOSE 8765
HEALTHCHECK --interval=60s --timeout=5s CMD wget -qO- http://127.0.0.1:8765/health || exit 1
CMD ["python", "-u", "app.py"]
