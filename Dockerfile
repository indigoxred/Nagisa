FROM python:3.13.13-slim-bookworm@sha256:355bfa66770995d7e9a0da4b3473b44d0cb451f6b56f5615ad9c39e3c4eca03f

LABEL org.opencontainers.image.source="https://github.com/indigoxred/nagisa" \
      org.opencontainers.image.title="Nagisa" \
      org.opencontainers.image.description="Fail-closed Shoko ordering and Sonarr reconciliation"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/nagisa/src \
    CONFIG_DIR=/config \
    PORT=8787

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg=7:5.1.9-0+deb12u1 \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /config \
    && chown 65532:65532 /config

WORKDIR /opt/nagisa
COPY src ./src

USER 65532:65532
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD ["python", "-m", "nagisa", "healthcheck"]
ENTRYPOINT ["python", "-m", "nagisa"]
CMD ["run"]
