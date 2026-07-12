# translate.Dockerfile — lightweight image for the token-saver proxy pipeline.
# Built by build-and-install and tagged as localhost/translate-token-saver:latest.
#
# podman build -f vendor/translate.Dockerfile -t localhost/translate-token-saver:latest .

FROM python:3.12-slim

RUN pip install --no-cache-dir deep-translator==1.11.4

COPY lib/translate_proxy.py /translate_proxy.py
RUN chmod 644 /translate_proxy.py

# Feature flags — all default ON except heavy modules
ENV FEATURE_TRANSLATE=1
ENV FEATURE_STRIP_RESPONSE=1
ENV FEATURE_STRIP_SYSTEM=1
ENV FEATURE_MINIFY_TOOLS=1
ENV FEATURE_SUMMARIZE=0
ENV FEATURE_PROMPT_CACHE=0
ENV FEATURE_ROUTER=0

# Summarizer / router use these models
ENV SUMMARIZE_MODEL=deepseek-chat
ENV ROUTER_CHEAP_MODEL=deepseek-chat

# Network
ENV TRANSLATE_LISTEN_PORT=8786
ENV HEADROOM_PORT=8787
ENV HEADROOM_HOST=127.0.0.1

# Cache directory
ENV CACHE_DIR=/tmp/token-saver-cache

EXPOSE 8786

HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8786/readyz')" || exit 1

ENTRYPOINT ["python3", "-u", "/translate_proxy.py"]
