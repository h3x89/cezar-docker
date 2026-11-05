FROM node:24-bookworm-slim@sha256:a5f4366e834695d5f363b920c104953a535d3a9864d1bdf74566a0ebe01daee5

ARG CEZAR_VERSION=0.18.0
LABEL org.opencontainers.image.title="Cezar" \
      org.opencontainers.image.source="https://github.com/h3x89/cezar-docker" \
      org.opencontainers.image.version="${CEZAR_VERSION}"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git openssh-client bash curl python3 ripgrep \
    && install -d -m 0755 /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /etc/apt/keyrings/github-cli.gpg \
    && echo 'deb [arch=amd64 signed-by=/etc/apt/keyrings/github-cli.gpg] https://cli.github.com/packages stable main' > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/* \
    && usermod --login cezar --home /home/cezar node \
    && groupmod --new-name cezar node \
    && install -d -o 1000 -g 1000 -m 0700 /home/cezar /home/cezar/.codex \
    && install -d -o 1000 -g 1000 /home/cezar/.local /workspace/project \
    && printf '%s\n' 'cli_auth_credentials_store = "file"' > /home/cezar/.codex/config.toml \
    && chown 1000:1000 /home/cezar/.codex/config.toml \
    && chmod 0600 /home/cezar/.codex/config.toml

WORKDIR /opt/cezar
RUN node -e 'if (!/^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$/.test(process.argv[1])) process.exit(1)' "${CEZAR_VERSION}" \
    && npm install --omit=dev --no-audit --no-fund --save-exact "@open-mercato/cezar@${CEZAR_VERSION}" \
    && test "$(node -p 'require("@open-mercato/cezar/package.json").version')" = "${CEZAR_VERSION}" \
    && npm cache clean --force

COPY --chmod=0755 scripts/codex-tool /usr/local/bin/codex-tool
ENV HOME=/home/cezar \
    CEZ_HOME=/home/cezar/.cezar \
    CODEX_HOME=/home/cezar/.codex \
    PATH="/home/cezar/.local/bin:/opt/cezar/node_modules/.bin:${PATH}"

USER 1000:1000
WORKDIR /workspace/project
EXPOSE 4321
STOPSIGNAL SIGTERM
ENTRYPOINT ["node", "/opt/cezar/node_modules/@open-mercato/cezar/dist/index.js"]
CMD ["--no-open", "--port", "4321", "--bind-host", "0.0.0.0", "--repo", "/workspace/project"]
