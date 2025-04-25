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
    
    Skips container creation if running in GitHub Actions CI environment.
    """
    # Check if running in GitHub Actions CI
    is_github_ci = os.environ.get("GITHUB_ACTIONS") == "true"
    
    # If in CI, we'll use the GitHub Actions service container instead
    if is_github_ci:
        print("Running in GitHub Actions CI - using GitHub-provided MinIO service")
        # Verify the CI-provided MinIO is available
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.connect(("localhost", 9000))
            print("CI MinIO service is running and responding")
            s.close()
        except Exception as conn_error:
            pytest.fail(f"CI MinIO service not responding on port 9000: {conn_error}")
        yield None
        return
    
    # Get Docker host from environment if available
    docker_host = os.environ.get("DOCKER_HOST")
    if docker_host:
        print(f"Using DOCKER_HOST: {docker_host}")
    
    # Check for and clean up any existing container with our name
    container_name = "alcove-minio-test"
    
    try:
        client = docker.from_env()
        
        # Try to remove any existing container
        try:
            old_container = client.containers.get(container_name)
            if old_container.status == "running":
                old_container.stop()
            old_container.remove()
            print(f"Removed existing container: {container_name}")
        except NotFound:
            pass  # Container doesn't exist, which is fine
        
        # Create a new container
        container = client.containers.run(
            "minio/minio",
            name=container_name,
            command="server /data",
            environment={
                "MINIO_ROOT_USER": "justtesting",
                "MINIO_ROOT_PASSWORD": "justtesting",
            },
            ports={"9000/tcp": 9000},
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
        except NotFound:
            pass
            
        # Create the test bucket
        bucket_container = client.containers.run(
            "minio/mc",
            name="alcove-createbucket",
            entrypoint=["/bin/sh", "-c"],
            command=[f"mc config host add myminio http://{container_name}:9000 justtesting justtesting && mc mb myminio/test -p || true"],
            network_mode="default", 
            links={container_name: container_name},
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
            pytest.fail(f"MinIO container is not responding on port 9000: {conn_error}")
        
        # Successfully initialized Docker
        print("Using Docker-managed MinIO for testing")
        
        # Yield the container for the tests to use
        yield container
        
        # Clean up after tests
        print("Cleaning up MinIO container")
        container.stop()
        container.remove()
        
    except Exception as e:
        error_msg = f"Docker or MinIO not available: {str(e)}"
        pytest.fail(error_msg)


@pytest.fixture
def setup_test_environment(tmp_path, minio_container):
    """
    Setup test environment with the MinIO container running.
    This replaces the fixtures in test_alcove.py and test_tables.py.
    
    Uses the same configuration for both local testing and GitHub CI environment.
    """
    # If MinIO is required but not available, tests will have already failed through the minio_container fixture
    
    # Setup test environment with consistent S3 credentials for both local and CI
    os.environ["TEST_ENVIRONMENT"] = "1"  # Enable test mode
    os.environ["S3_ACCESS_KEY"] = "justtesting"
    os.environ["S3_SECRET_KEY"] = "justtesting"
    os.environ["S3_BUCKET_NAME"] = "test"
    os.environ["S3_ENDPOINT_URL"] = "http://localhost:9000"

    # Create test directory and files
    test_dir = tmp_path / "test_dir"
    test_dir.mkdir()

    # Change to test directory
    os.chdir(test_dir)

    yield test_dir

    # Cleanup
    shutil.rmtree(test_dir)