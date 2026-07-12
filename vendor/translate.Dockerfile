# translate.Dockerfile — minimal image for the translate proxy layer.
# Built by build-and-install and tagged as localhost/translate-token-saver:latest.
#
# docker/podman build -f vendor/translate.Dockerfile -t localhost/translate-token-saver:latest .

FROM python:3.12-slim

RUN pip install --no-cache-dir deep-translator==1.11.4

COPY lib/translate_proxy.py /translate_proxy.py

ENV TRANSLATE_ENABLED=1
ENV TRANSLATE_LISTEN_PORT=8786
ENV HEADROOM_PORT=8787
ENV HEADROOM_HOST=127.0.0.1

EXPOSE 8786

HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8786/readyz')" || exit 1

ENTRYPOINT ["python3", "-u", "/translate_proxy.py"]
