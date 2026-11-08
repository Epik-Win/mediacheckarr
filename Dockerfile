FROM node:20-alpine AS web-build
WORKDIR /src/webserver
RUN corepack enable && corepack prepare pnpm@9.15.9 --activate
COPY webserver/package.json webserver/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY webserver/ ./
RUN pnpm build

FROM golang:1.25-alpine AS go-build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
COPY --from=web-build /src/webserver/build ./webserver/build
RUN CGO_ENABLED=0 go build -trimpath -o /checkrr .

FROM alpine:3.22
LABEL org.opencontainers.image.title="mediacheckarr"
RUN apk add --no-cache bash ca-certificates ffmpeg python3 py3-yaml tzdata
COPY --from=go-build /checkrr /checkrr
COPY mediacheck /opt/mediacheck
COPY docker/entrypoint.sh /usr/local/bin/mediacheckarr-entrypoint
RUN chmod +x /usr/local/bin/mediacheckarr-entrypoint /opt/mediacheck/mediacheck \
    /opt/mediacheck/identity_check.py /opt/mediacheck/fix_mkv_audio.sh \
    /opt/mediacheck/fix_mp4_interleave.sh
ENV CHECKRR_CONFIG=/config/checkrr.yaml \
    MEDIACHECK_CONFIG_DIR=/config \
    MEDIACHECK_DATA_DIR=/data \
    PYTHONUNBUFFERED=1
EXPOSE 8585
ENTRYPOINT ["/usr/local/bin/mediacheckarr-entrypoint"]