"""Reliability helpers for filesystem operations.

 Implementation:
-: Atomic writes (temp file + rename pattern)
-: HEREDOC delimiter collision prevention
-: Backup before overwrite

These helpers ensure filesystem operations are robust against:
- Power failures / process kills during writes
- Content containing HEREDOC delimiters
- Accidental data loss from overwrites"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Default backup suffix pattern
BACKUP_SUFFIX = ".bak"
TIMESTAMPED_BACKUP_PATTERN = ".{timestamp}.bak"


def atomic_write_text(
    target: Path,
    content: str,
    encoding: str = "utf-8",
) -> None:
    """Write content atomically using temp file + rename pattern.
    
    This ensures that the target file is either:
    - Completely written with new content, OR
    - Unchanged (on failure)
    
    The operation is atomic on POSIX filesystems when temp and target
    are on the same filesystem. On Windows, it uses replace() which
    provides similar guarantees.
    
    Args:
        target: Destination file path
        content: Text content to write
        encoding: Text encoding (default: utf-8)
        
    Raises:
        OSError: If write or rename fails
        
    Example:
        >>> atomic_write_text(Path("config.yaml"), "key: value")
        # Either config.yaml has new content or is unchanged
    """
    # Create temp file in same directory as target for atomic rename
    target_dir = target.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    
    # Use a predictable but unique temp filename
    temp_suffix = f".tmp.{os.getpid()}.{int(time.time() * 1000)}"
    temp_path = target.with_suffix(target.suffix + temp_suffix)
    
    try:
        # Write to temp file with explicit open for fsync support
        with temp_path.open("w", encoding=encoding, newline="") as f:
            f.write(content)
            f.flush()
            # Sync to disk (ensures durability)
            # On Windows, fsync may not be supported on all file systems
            try:
                os.fsync(f.fileno())
            except OSError:
                pass  # Best effort - some Windows filesystems don't support fsync
        
        # Atomic rename (replace on Windows/POSIX)
        temp_path.replace(target)
        
        logger.debug(f"Atomic write completed: {target}")
        
    except Exception:
        # Clean up temp file on failure
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass  # Best effort cleanup
        raise


def atomic_write_bytes(
    target: Path,
    data: bytes,
) -> None:
    """Write binary data atomically using temp file + rename pattern.
    
    Args:
        target: Destination file path
        data: Binary data to write
        
    Raises:
        OSError: If write or rename fails
    """
    target_dir = target.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    
    temp_suffix = f".tmp.{os.getpid()}.{int(time.time() * 1000)}"
    temp_path = target.with_suffix(target.suffix + temp_suffix)
    
    try:
        # Write with explicit open for fsync support
        with temp_path.open("wb") as f:
            f.write(data)
            f.flush()
            # Sync to disk (ensures durability)
            try:
                os.fsync(f.fileno())
            except OSError:
                pass  # Best effort - some filesystems don't support fsync
        
        temp_path.replace(target)
        logger.debug(f"Atomic binary write completed: {target}")
        
    except Exception:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        raise


def create_backup(
    target: Path,
    *,
    timestamped: bool = False,
) -> Optional[Path]:
    """Create a backup copy of a file before modification.
    
    Args:
        target: File to back up
        timestamped: If True, include timestamp in backup name
                     (allows multiple backups). If False, use simple .bak
                     suffix (overwrites previous backup).
    
    Returns:
        Path to backup file, or None if target doesn't exist
        
    Raises:
        OSError: If backup creation fails
        
    Examples:
        >>> create_backup(Path("config.yaml"))
        PosixPath('config.yaml.bak')
        
        >>> create_backup(Path("config.yaml"), timestamped=True)
        PosixPath('config.yaml.1705512345.bak')
    """
    if not target.exists():
        return None
    
    if timestamped:
        timestamp = int(time.time())
        backup_path = target.with_suffix(
            target.suffix + TIMESTAMPED_BACKUP_PATTERN.format(timestamp=timestamp)
        )
    else:
        backup_path = target.with_suffix(target.suffix + BACKUP_SUFFIX)
    
    # Use shutil.copy2 to preserve metadata
    shutil.copy2(target, backup_path)
    logger.debug(f"Created backup: {target} -> {backup_path}")
    
    return backup_path


def restore_from_backup(
    target: Path,
    backup_path: Optional[Path] = None,
) -> bool:
    """Restore a file from its backup.
    
    Args:
        target: Original file path to restore
        backup_path: Explicit backup path, or None to use default .bak
        
    Returns:
        True if restore succeeded, False if no backup exists
    """
    if backup_path is None:
        backup_path = target.with_suffix(target.suffix + BACKUP_SUFFIX)
    
    if not backup_path.exists():
        return False
    
    shutil.copy2(backup_path, target)
    logger.debug(f"Restored from backup: {backup_path} -> {target}")
    return True


def generate_safe_heredoc_delimiter(
    content: str,
    base: str = "DROWAI_EOF",
) -> str:
    """Generate a HEREDOC delimiter that doesn't appear in content.
    
    This prevents content truncation when using heredoc syntax in shell
    commands. If the base delimiter appears in content, we append a
    counter until we find a safe variant.
    
    Args:
        content: The content that will be written
        base: Base delimiter string to start with
        
    Returns:
        A delimiter string guaranteed not to appear in content
        
    Examples:
        >>> generate_safe_heredoc_delimiter("hello world")
        'DROWAI_EOF'
        
        >>> generate_safe_heredoc_delimiter("DROWAI_EOF appears here")
        'DROWAI_EOF_1'
        
        >>> generate_safe_heredoc_delimiter("DROWAI_EOF and DROWAI_EOF_1")
        'DROWAI_EOF_2'
    """
    delimiter = base
    counter = 0
    
    # Check if delimiter appears as a line by itself or at boundaries
    # The heredoc terminates when the delimiter appears on its own line
    while _delimiter_appears_in_content(delimiter, content):
        counter += 1
        delimiter = f"{base}_{counter}"
        
        # Safety limit to prevent infinite loop on adversarial input
        if counter > 1000:
            # Fall back to hash-based delimiter
            content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
            delimiter = f"{base}_{content_hash}"
            break
    
    return delimiter


def _delimiter_appears_in_content(delimiter: str, content: str) -> bool:
    """Check if delimiter would cause heredoc termination.
    
    A heredoc terminates when the delimiter appears on its own line.
    We check for:
    - Delimiter at start of content (no preceding newline)
    - Delimiter on a line by itself
    - Delimiter at end without trailing newline
    """
    # Check if delimiter appears on its own line
    if f"\n{delimiter}\n" in content:
        return True
    
    # Check if content starts with delimiter on its own line
    if content.startswith(f"{delimiter}\n"):
        return True
    
    # Check if content ends with delimiter (possibly on last line)
    if content.endswith(f"\n{delimiter}"):
        return True
    
    # Check if entire content is just the delimiter
    if content == delimiter:
        return True
    
    return False


def build_safe_heredoc_command(
    path: str,
    content: str,
    append: bool = False,
) -> str:
    """Build a heredoc command with collision-safe delimiter.
    
    Args:
        path: Target file path (should be shell-quoted by caller)
        content: Content to write
        append: If True, use >> instead of >
        
    Returns:
        Shell command string using safe heredoc syntax
    """
    import shlex
    
    delimiter = generate_safe_heredoc_delimiter(content)
    redirect = ">>" if append else ">"
    
    # Quote the path for safety
    quoted_path = shlex.quote(path)
    
    # Use quoted delimiter to prevent variable expansion
    command = (
        f"cat {redirect} {quoted_path} << '{delimiter}'\n"
        f"{content}\n"
        f"{delimiter}"
    )
    
    return command


class AtomicWriteContext:
    """Context manager for atomic file operations with automatic rollback.
    
    Use this when you need to perform multiple operations that should
    either all succeed or all fail (transaction-like behavior).
    
    Example:
        >>> with AtomicWriteContext(Path("config.yaml")) as ctx:
        ...     ctx.write("key: value")
        ...     # If any exception occurs, original file is restored
    """
    
    def __init__(self, target: Path, backup: bool = True):
        self.target = target
        self.backup = backup
        self.backup_path: Optional[Path] = None
        self._committed = False
    
    def __enter__(self) -> "AtomicWriteContext":
        if self.backup and self.target.exists():
            self.backup_path = create_backup(self.target)
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        if exc_type is not None and not self._committed:
            # Exception occurred, restore backup
            if self.backup_path and self.backup_path.exists():
                try:
                    restore_from_backup(self.target, self.backup_path)
                    logger.info(f"Restored {self.target} from backup after error")
                except OSError as e:
                    logger.error(f"Failed to restore backup: {e}")
        
        # Clean up backup on success (if not keeping it)
        if exc_type is None and self.backup_path and self.backup_path.exists():
            # Keep the backup for now - let caller decide cleanup
            pass
        
        return False  # Don't suppress exceptions
    
    def write(self, content: str, encoding: str = "utf-8") -> None:
        """Write content atomically."""
        atomic_write_text(self.target, content, encoding)
    
    def commit(self) -> None:
        """Mark the operation as committed (prevent rollback)."""
        self._committed = True
