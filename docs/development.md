# Development

## Testing with MinIO

For testing with S3-compatible storage, this project uses automatically managed containers:

```bash
# Run tests with Docker-based MinIO
make test
```

All tests require Docker with MinIO container to be available.

## Docker Context Support

The testing framework automatically detects your current Docker context and uses it for container operations. This ensures tests work properly with:

- Docker Desktop
- Colima
- OrbStack
- Remote Docker contexts

## MinIO Configuration

With Docker, these credentials are automatically used:

| Setting | Value |
|---------|-------|
| Access Key | `minioadmin` |
| Secret Key | `minioadmin` |
| Bucket | `test-bucket` |
| Endpoint | `http://localhost:9000` |

Containers are automatically managed and kept running between test runs for performance.
MinIO's health is verified before tests run to ensure proper S3 compatibility.
