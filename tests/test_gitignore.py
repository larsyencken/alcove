import os
import tempfile
from pathlib import Path
from unittest import mock

from alcove import audit_gitignore_setup
from alcove.utils import add_to_gitignore, add_to_data_files, ensure_data_files_in_gitignore


def test_gitignore_simple():
    """Test basic functionality of add_to_gitignore"""
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
            
            # Test with non-existent .gitignore
            add_to_gitignore(test_file)
            
            # Check the file was created with the right entry
            gitignore_path = tmp_path / ".gitignore"
            assert gitignore_path.exists()
            content = gitignore_path.read_text().strip()
            assert content == "data/test/path"
            
            # Test adding a second entry
            test_file2 = data_dir / "another" / "path"
            test_file2.parent.mkdir(parents=True)
            test_file2.touch()
            add_to_gitignore(test_file2)
            
            # Check the new entry was added
            content = gitignore_path.read_text().strip().split('\n')
            assert "data/test/path" in content
            assert "data/test/another/path" in content


def test_gitignore_existing_entry():
    """Test add_to_gitignore with an already existing entry"""
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        tmp_path = Path(tmp)
        
        # Mock BASE_DIR to be our temporary directory
        with mock.patch('alcove.utils.BASE_DIR', tmp_path):
            # Create .gitignore with existing entry
            gitignore_path = tmp_path / ".gitignore"
            gitignore_path.write_text("existing/entry\n")
            
            # Create a file that matches the existing entry
            entry_path = tmp_path / "existing" / "entry"
            entry_path.parent.mkdir(parents=True)
            entry_path.touch()
            
            # Try to add the same entry
            add_to_gitignore(entry_path)
            
            # Check no duplicate was added
            content = gitignore_path.read_text().strip().split('\n')
            assert len(content) == 1
            assert "existing/entry" in content


def test_ensure_data_files_in_gitignore():
    """Test that ensure_data_files_in_gitignore works correctly"""
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        
        # Test with non-existent .gitignore
        ensure_data_files_in_gitignore()
        
        # Check the file was created with the right entry
        gitignore_path = Path(tmp) / ".gitignore"
        assert gitignore_path.exists()
        content = gitignore_path.read_text().strip()
        assert content == ".data-files"
        
        # Test idempotence - calling again shouldn't add duplicate
        ensure_data_files_in_gitignore()
        content = gitignore_path.read_text().strip()
        assert content == ".data-files"


def test_add_to_data_files():
    """Test that add_to_data_files works correctly"""
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
            
            # Test with non-existent .data-files
            add_to_data_files(test_file)
            
            # Check both files were created with the right entries
            gitignore_path = tmp_path / ".gitignore"
            data_files_path = tmp_path / ".data-files"
            
            assert gitignore_path.exists()
            assert data_files_path.exists()
            
            gitignore_content = gitignore_path.read_text().strip()
            data_files_content = data_files_path.read_text().strip()
            
            assert gitignore_content == ".data-files"
            assert data_files_content == "data/test/path"
            
            # Add another entry
            test_file2 = data_dir / "another" / "path"
            test_file2.parent.mkdir(parents=True)
            test_file2.touch()
            add_to_data_files(test_file2)
            
            # Check the new entry was added to .data-files only
            data_files_content = data_files_path.read_text().strip().split('\n')
            assert "data/test/path" in data_files_content
            assert "data/test/another/path" in data_files_content
            
            # Check .gitignore remains unchanged
            gitignore_content = gitignore_path.read_text().strip()
            assert gitignore_content == ".data-files"
        
        
def test_audit_gitignore_setup():
    """Test that audit_gitignore_setup works correctly"""
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        
        # Create a .gitignore with data file patterns
        gitignore_path = Path(tmp) / ".gitignore"
        gitignore_path.write_text("data/snapshots/test/path\ndata/snapshots/another/path\nother/file\n")
        
        # Run audit with fix=True
        audit_gitignore_setup(fix=True)
        
        # Check that .data-files was created and contains the data patterns
        data_files_path = Path(tmp) / ".data-files"
        assert data_files_path.exists()
        
        data_files_content = data_files_path.read_text().strip().split("\n")
        assert "data/snapshots/test/path" in data_files_content
        assert "data/snapshots/another/path" in data_files_content
        
        # Check that .gitignore now only contains non-data patterns and .data-files
        gitignore_content = gitignore_path.read_text().strip().split("\n")
        assert ".data-files" in gitignore_content
        assert "other/file" in gitignore_content
        assert "data/snapshots/test/path" not in gitignore_content
        assert "data/snapshots/another/path" not in gitignore_content