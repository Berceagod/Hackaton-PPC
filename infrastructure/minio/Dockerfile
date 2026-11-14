# MinIO no longer publishes prebuilt images (Docker Hub/quay), so we build the
# S3 server from its (public) source. Pinned via MINIO_REF; override with --build-arg.
FROM golang:1.24-alpine AS build
ARG MINIO_REF=latest
ENV GOTOOLCHAIN=auto CGO_ENABLED=0
RUN apk add --no-cache git
RUN go install -trimpath -ldflags "-s -w" github.com/minio/minio@${MINIO_REF}

FROM alpine:3.20
RUN apk add --no-cache ca-certificates curl && adduser -D -u 10001 minio && mkdir /data && chown minio /data
COPY --from=build /go/bin/minio /usr/local/bin/minio
USER minio
VOLUME /data
EXPOSE 9000
HEALTHCHECK --interval=5s --timeout=3s --retries=20 CMD curl -sf http://127.0.0.1:9000/minio/health/live || exit 1
ENTRYPOINT ["minio"]
CMD ["server", "/data"]
