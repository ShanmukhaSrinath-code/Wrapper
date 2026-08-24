# minio — S3-compatible object storage

| | |
|---|---|
| Image | `minio/minio` |
| Ports | 9000 (S3 API), 9001 (console) |
| Used by | app, worker |
| Config | environment (in `deploy/docker-compose.yml`) |

No config file: MinIO is configured entirely by environment and by the app,
which creates its bucket on startup (`Storage.ensure_ready`).

Because MinIO speaks the S3 API, the same adapter (`app/core/storage/minio.py`)
targets real S3 by changing the endpoint and credentials — call sites depend on
the `Storage` interface, never on boto3.
