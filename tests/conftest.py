import docker
from docker.errors import NotFound
import os
import pytest
import time
import shutil


@pytest.fixture(scope="session")
def minio_container():
    """
    Start a MinIO container for testing, using the Docker SDK.
    Raises an exception if Docker is not available or container can't be started.
    
    Uses the DOCKER_HOST environment variable if set to connect to the correct Docker context.
    """
    # Check if we want to force MinIO container to be used
    require_minio = os.environ.get("REQUIRE_MINIO", "1") == "1"
    
    # Get Docker host from environment if available
    docker_host = os.environ.get("DOCKER_HOST")
    if docker_host:
        print(f"Using DOCKER_HOST: {docker_host}")
    
    try:
        client = docker.from_env()
        
        # Check if the container is already running
        try:
            container = client.containers.get("alcove-minio-test")
            # If container exists but is not running, start it
            if container.status != "running":
                container.start()
        except NotFound:
            # Create and start a new container
            container = client.containers.run(
                "minio/minio",
                name="alcove-minio-test",
                command="server /data --console-address :9001",
                environment={
                    "MINIO_ROOT_USER": "minioadmin",
                    "MINIO_ROOT_PASSWORD": "minioadmin",
                },
                ports={"9000/tcp": 9000, "9001/tcp": 9001},
                volumes={"/tmp/minio-data": {"bind": "/data", "mode": "rw"}},
                detach=True,
            )
        
        # Wait for MinIO to be ready
        time.sleep(3)
        
        # Create the test bucket using another container
        try:
            bucket_container = client.containers.get("alcove-createbucket")
            if bucket_container.status != "exited":
                bucket_container.remove(force=True)
                raise NotFound("Container exists but not exited")
        except NotFound:
            # MinIO mc client doesn't support 'sh -c', use direct commands
            bucket_container = client.containers.run(
                "minio/mc",
                name="alcove-createbucket",
                entrypoint=["/bin/sh", "-c"],
                command=["mc config host add myminio http://alcove-minio-test:9000 minioadmin minioadmin && mc mb myminio/test-bucket -p || true"],
                network_mode="default", 
                links={"alcove-minio-test": "alcove-minio-test"},
                detach=False,
                remove=True,
            )
        
        # Verify MinIO is actually responding
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.connect(("localhost", 9000))
            print("MinIO container is running and responding")
            s.close()
        except Exception as conn_error:
            if require_minio:
                pytest.fail(f"MinIO container is not responding on port 9000: {conn_error}")
            else:
                print(f"WARNING: MinIO container not responding on port 9000: {conn_error}")
        
        # Successfully initialized Docker
        print("Using Docker-managed MinIO for testing")
        yield container
        
        # Don't stop the container after tests - leave it running for faster subsequent test runs
        # If you want to clean up: container.stop() and container.remove()
        
    except Exception as e:
        error_msg = f"Docker or MinIO not available: {str(e)}"
        if require_minio:
            pytest.fail(error_msg)
        else:
            print(f"WARNING: {error_msg}")
            print("Tests will be skipped or may fail if they require S3")
            yield None


@pytest.fixture
def setup_test_environment(tmp_path, minio_container):
    """
    Setup test environment with the MinIO container running.
    This replaces the fixtures in test_alcove.py and test_tables.py.
    """
    # If MinIO is required but not available, tests will have already failed through the minio_container fixture
    
    # Setup test environment with fixed S3 credentials for containerized MinIO
    os.environ["TEST_ENVIRONMENT"] = "1"  # Enable test mode
    os.environ["S3_ACCESS_KEY"] = "minioadmin"
    os.environ["S3_SECRET_KEY"] = "minioadmin"
    os.environ["S3_BUCKET_NAME"] = "test-bucket"
    os.environ["S3_ENDPOINT_URL"] = "http://localhost:9000"

    # Create test directory and files
    test_dir = tmp_path / "test_dir"
    test_dir.mkdir()

    # Change to test directory
    os.chdir(test_dir)

    yield test_dir

    # Cleanup
    shutil.rmtree(test_dir)