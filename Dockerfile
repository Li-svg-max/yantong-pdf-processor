FROM debian:bookworm-slim AS downloader

ARG CLI_PROXY_VERSION=7.2.144
ARG CLI_PROXY_SHA256=02be1ad96791f1d2b7e6574bb0f68a3d75622e42cba07fecd012e575ba4b2a96

RUN apt-get update \
  && apt-get install -y --no-install-recommends ca-certificates curl tar \
  && rm -rf /var/lib/apt/lists/*

RUN curl --fail --show-error --location --retry 3 \
      "https://github.com/router-for-me/CLIProxyAPI/releases/download/v${CLI_PROXY_VERSION}/CLIProxyAPI_${CLI_PROXY_VERSION}_linux_amd64.tar.gz" \
      --output /tmp/cli-proxy-api.tar.gz \
  && echo "${CLI_PROXY_SHA256}  /tmp/cli-proxy-api.tar.gz" | sha256sum -c - \
  && mkdir -p /opt/cli-proxy \
  && tar -xzf /tmp/cli-proxy-api.tar.gz -C /opt/cli-proxy \
  && test -x /opt/cli-proxy/cli-proxy-api

FROM debian:bookworm-slim

RUN apt-get update \
  && apt-get install -y --no-install-recommends ca-certificates passwd \
  && rm -rf /var/lib/apt/lists/* \
  && groupadd --system --gid 10001 cliproxy \
  && useradd --system --uid 10001 --gid 10001 --home /data --shell /usr/sbin/nologin cliproxy \
  && mkdir -p /data/auth /app \
  && chown -R cliproxy:cliproxy /data /app

COPY --from=downloader /opt/cli-proxy/cli-proxy-api /usr/local/bin/cli-proxy-api
COPY --chown=cliproxy:cliproxy docker-entrypoint.sh /app/docker-entrypoint.sh

RUN chmod 0555 /usr/local/bin/cli-proxy-api /app/docker-entrypoint.sh

USER cliproxy
WORKDIR /app
EXPOSE 8080

ENTRYPOINT ["/app/docker-entrypoint.sh"]
