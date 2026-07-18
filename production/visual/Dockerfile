# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
FROM mcr.microsoft.com/playwright@sha256:cf0daee9b994042e011bc29f20cdff1a9f682a039b43fcd738f7d8a9d3bcd9d6

ARG AURIS_VISUAL_RUNNER_CONTRACT_SHA256
ENV AURIS_VISUAL_RUNNER_CONTRACT_SHA256=${AURIS_VISUAL_RUNNER_CONTRACT_SHA256} \
    AURIS_VISUAL_RUNNER_IMAGE=mcr.microsoft.com/playwright@sha256:cf0daee9b994042e011bc29f20cdff1a9f682a039b43fcd738f7d8a9d3bcd9d6 \
    AURIS_VISUAL_RUNNER_PLATFORM=linux/amd64 \
    AURIS_VISUAL_REPRODUCIBILITY_SCOPE="exact Playwright Linux image digest, linux/amd64, npm lock, browser and image fonts"

LABEL org.opencontainers.image.title="Auris Flow visual release runner" \
      org.opencontainers.image.source="https://github.com/auris-flow/auris-flow" \
      io.auris.visual.runner-contract-sha256=${AURIS_VISUAL_RUNNER_CONTRACT_SHA256}

WORKDIR /opt/auris-visual

COPY prototype/auris-flow-ui/package.json prototype/auris-flow-ui/package-lock.json ./
RUN npm ci --ignore-scripts \
    && test "$(node -p "require('./node_modules/playwright/package.json').version")" = "1.61.1" \
    && npm cache clean --force

COPY prototype/auris-flow-ui/audit/visual-regression.config.mjs ./audit/visual-regression.config.mjs
COPY prototype/auris-flow-ui/audit/visual-regression.spec.mjs ./audit/visual-regression.spec.mjs
COPY production/visual/runtime.mjs ./runtime.mjs

ENTRYPOINT ["npx", "playwright", "test", "-c", "audit/visual-regression.config.mjs", "--workers=1"]
