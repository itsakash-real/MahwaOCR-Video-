# Optional. Docker is NOT needed for session isolation -- sessions are just
# directories and already isolate cleanly on the host.
#
# Use this when you want a pinned Python environment, or to run on a server.
#
# Note: this suits PROVIDER mode (the tool calls a vision API itself). It fits
# agent mode poorly, because in agent mode the agent runs on your host and would
# need to reach tiles inside the container. For agent mode, run on the host.

FROM python:3.12-slim

RUN useradd --create-home --uid 1000 app

WORKDIR /app
COPY pyproject.toml README.md ./
COPY manhwa_ocr ./manhwa_ocr
RUN pip install --no-cache-dir .

# Sessions live on a mounted volume so data outlives the container.
ENV MANHWA_SESSIONS_ROOT=/data/sessions
RUN mkdir -p /data/sessions && chown -R app:app /data

USER app
VOLUME ["/data"]

ENTRYPOINT ["python", "-m", "manhwa_ocr"]
CMD ["--help"]
