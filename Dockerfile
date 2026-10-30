FROM python:3.12-slim
LABEL org.opencontainers.image.title="iCloud Photos MCP" \
      org.opencontainers.image.source="https://github.com/MephistoJB/iCloud-Photos-mcp"
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    HOME=/config USER=icloud-app ICLOUD_SESSION_DIR=/config/session \
    ICLOUD_PHOTOS_ENABLE_WRITES=0 ICLOUD_PHOTOS_ENABLE_DELETES=0
COPY dist/*.whl /tmp/wheels/
RUN set -eu; set -- /tmp/wheels/*.whl; test "$#" -eq 1; \
    python -m pip install --no-cache-dir "$1[cli,mcp]"; rm -rf /tmp/wheels
USER 99:100
EXPOSE 8080
VOLUME ["/config"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3)"
ENTRYPOINT ["icloud-photos-mcp-http"]
