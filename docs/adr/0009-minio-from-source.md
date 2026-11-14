# ADR-0009: MinIO is built from source
Status: accepted
Context: `minio/minio` no longer exists on Docker Hub and quay.io returns 401 (upstream stopped publishing images). The PDF allows MinIO as the S3 service.
Decision: `infrastructure/minio/Dockerfile` builds the MinIO server with `go install github.com/minio/minio@${MINIO_REF}` (default `latest`, overridable build arg) into an alpine image; first build takes ~2 min. Still a separate S3 service on the internal network.
Consequences: Slower first start; reproducible build needs a pinned `MINIO_REF` before the final demo (TODO in slice K). Fallback if the source disappears: SeaweedFS (`chrislusf/seaweedfs`, pulls fine).
