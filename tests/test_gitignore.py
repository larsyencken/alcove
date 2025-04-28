import os
import tempfile
from pathlib import Path
from unittest import mock

from alcove import audit_gitignore_setup
from alcove.utils import add_to_gitignore, add_to_data_gitignore, ensure_data_gitignore


def test_gitignore_simple():
    """Test basic functionality of add_to_gitignore (which now adds to data/.gitignore)"""
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        tmp_path = Path(tmp)
        
        # Mock BASE_DIR to be our temporary directory
        with mock.patch('alcove.utils.BASE_DIR', tmp_path):
            # Create a temporary path structure
            data_dir = tmp_path / "data" / "test"
            data_dir.mkdir(parents=True)
            test_file = data_dir / "path"
            test_file.touch()
            
            # Test with non-existent data/.gitignore
            add_to_gitignore(test_file)
            
            # Check the file was created with the right entry
            data_gitignore_path = tmp_path / "data" / ".gitignore"
            assert data_gitignore_path.exists()
            content = data_gitignore_path.read_text().strip().split('\n')
            assert "tables/" in content
            assert "test/path" in content  # path without "data/" prefix
            
            # Test adding a second entry
            test_file2 = data_dir / "another" / "path"
            test_file2.parent.mkdir(parents=True)
            test_file2.touch()
            add_to_gitignore(test_file2)
            
            # Check the new entry was added
            content = data_gitignore_path.read_text().strip().split('\n')
            assert "tables/" in content
            assert "test/path" in content
            assert "test/another/path" in content


def test_gitignore_existing_entry():
    """Test add_to_gitignore with an already existing entry in data/.gitignore"""
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        tmp_path = Path(tmp)
        
        # Mock BASE_DIR to be our temporary directory
        with mock.patch('alcove.utils.BASE_DIR', tmp_path):
            # Create data/.gitignore with existing entry
            data_dir = tmp_path / "data"
            data_dir.mkdir(parents=True)
            data_gitignore_path = data_dir / ".gitignore"
            data_gitignore_path.write_text("tables/\nexisting/entry\n")
            
            # Create a file that matches the existing entry
            entry_path = tmp_path / "data" / "existing" / "entry"
            entry_path.parent.mkdir(parents=True)
            entry_path.touch()
            
            # Try to add the same entry
            add_to_gitignore(entry_path)
            
            # Check no duplicate was added
            content = data_gitignore_path.read_text().strip().split('\n')
            assert len(content) == 2
            assert "tables/" in content
            assert "existing/entry" in content


def test_ensure_data_gitignore():
    """Test that ensure_data_gitignore works correctly"""
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        
        # Test with non-existent data directory
        ensure_data_gitignore()
        
        # Check the directories and file were created with the right entries
        data_dir = Path(tmp) / "data"
        data_gitignore_path = data_dir / ".gitignore"
        
        assert data_dir.exists()
        assert data_gitignore_path.exists()
        
        content = data_gitignore_path.read_text().strip()
        assert content == "tables/"
        
        # Test idempotence - calling again shouldn't add duplicate
        ensure_data_gitignore()
        content = data_gitignore_path.read_text().strip()
        assert content == "tables/"


def test_add_to_data_gitignore():
    """Test that add_to_data_gitignore works correctly"""
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        tmp_path = Path(tmp)
        
        # Mock BASE_DIR to be our temporary directory
        with mock.patch('alcove.utils.BASE_DIR', tmp_path):
            # Create a test file
            data_dir = tmp_path / "data" / "test"
            data_dir.mkdir(parents=True)
            test_file = data_dir / "path"
            test_file.touch()
            
            # Test with non-existent data/.gitignore
            add_to_data_gitignore(test_file)
            
            # Check data/.gitignore was created with the right entries
            data_gitignore_path = tmp_path / "data" / ".gitignore"
            
            assert data_gitignore_path.exists()
            
            data_gitignore_content = data_gitignore_path.read_text().strip().split('\n')
            
            # Should contain tables/ and test/path (without data/ prefix)
            assert "tables/" in data_gitignore_content
            assert "test/path" in data_gitignore_content
            
            # Add another entry
            test_file2 = data_dir / "another" / "path"
            test_file2.parent.mkdir(parents=True)
            test_file2.touch()
            add_to_data_gitignore(test_file2)
            
            # Check the new entry was added to data/.gitignore
            data_gitignore_content = data_gitignore_path.read_text().strip().split('\n')
            assert "tables/" in data_gitignore_content
            assert "test/path" in data_gitignore_content
            assert "test/another/path" in data_gitignore_content
        
        
def test_audit_gitignore_setup():
    """Test that audit_gitignore_setup works correctly"""
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        
        # Create a .gitignore with data file patterns
        gitignore_path = Path(tmp) / ".gitignore"
        gitignore_path.write_text("data/snapshots/test/path\ndata/snapshots/another/path\nother/file\n")
        
        # Run audit with fix=True
        audit_gitignore_setup(fix=True)
        
        # Check that data/.gitignore was created and contains the right patterns
        data_dir = Path(tmp) / "data"
        data_gitignore_path = data_dir / ".gitignore"
        assert data_gitignore_path.exists()
        
        data_gitignore_content = data_gitignore_path.read_text().strip().split("\n")
        assert "tables/" in data_gitignore_content
        assert "snapshots/test/path" in data_gitignore_content
        assert "snapshots/another/path" in data_gitignore_content
        
        # Check that .gitignore now only contains non-data patterns
        gitignore_content = gitignore_path.read_text().strip().split("\n")
        assert "other/file" in gitignore_content
        assert "data/snapshots/test/path" not in gitignore_content
        assert "data/snapshots/another/path" not in gitignore_content
        
        # Test migration from .data-files to data/.gitignore
        # Create a .data-files file with some entries
        data_files_path = Path(tmp) / ".data-files"
        data_files_path.write_text("data/old/entry\ndata/another/old/entry\n")
        
        # Run audit with fix=True again
        audit_gitignore_setup(fix=True)
        
        # Check that .data-files was removed
        assert not data_files_path.exists()
        
        # Check that data/.gitignore now contains both sets of entries
        data_gitignore_content = data_gitignore_path.read_text().strip().split("\n")
        assert "tables/" in data_gitignore_content
        assert "snapshots/test/path" in data_gitignore_content
        assert "snapshots/another/path" in data_gitignore_content
        assert "old/entry" in data_gitignore_content
        assert "another/old/entry" in data_gitignore_content