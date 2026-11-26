# The community MinIO distribution is source-only. Build the pinned upstream release.
FROM golang:1.25-alpine AS build
RUN apk add --no-cache git ca-certificates
RUN CGO_ENABLED=0 go install -trimpath github.com/minio/minio@RELEASE.2025-10-15T17-29-55Z

FROM alpine:3.22
RUN apk add --no-cache ca-certificates \
    && adduser -D -u 10001 storage \
    && mkdir /data && chown storage:storage /data
COPY --from=build /go/bin/minio /usr/local/bin/minio
USER storage
EXPOSE 9000
ENTRYPOINT ["minio"]
CMD ["server", "/data"]
