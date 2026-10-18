"""Linux-only private file fallback. Values are plaintext, protected by permissions."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid


def _check(fd, directory=False):
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or (not directory and info.st_nlink != 1):
        raise ValueError('Credential storage ownership or link count is unsafe')
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise ValueError('Credential storage type is unsafe')
    if stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600):
        raise ValueError('Credential storage permissions must be 0700/0600')


@contextmanager
def locked_account(account):
    import fcntl
    from mino_scout.config import config_dir
    root = Path(config_dir()) / 'plugin-secrets'
    root.parent.mkdir(parents=True, exist_ok=True)
    try:
        root.mkdir(mode=0o700)
    except FileExistsError:
        pass
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    lock = None
    try:
        _check(directory, True)
        name = hashlib.sha256(account.encode('utf-8')).hexdigest()
        lock = os.open(name + '.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        _check(lock)
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield Entry(directory, name + '.json')
    finally:
        if lock is not None:
            os.close(lock)
        os.close(directory)


class Entry:
    def __init__(self, directory, name):
        self.directory, self.name = directory, name

    def read(self):
        """Return (exists, value); None is a deletion tombstone, never a lookup miss."""
        try:
            fd = os.open(self.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.directory)
        except FileNotFoundError:
            return False, None
        try:
            _check(fd)
            with os.fdopen(fd, 'r', encoding='utf-8', closefd=False) as stream:
                data = json.load(stream)
            if not isinstance(data, dict) or data.get('version') != 1 or 'value' not in data or (data['value'] is not None and not isinstance(data['value'], str)):
                raise ValueError('Invalid credential file')
            return True, data['value']
        finally:
            os.close(fd)

    def write(self, value):
        # Validate an existing destination rather than replacing an unsafe link/file.
        self.read()
        temporary = '.' + self.name + '.' + uuid.uuid4().hex
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.directory)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump({'version': 1, 'value': value}, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.name, src_dir_fd=self.directory, dst_dir_fd=self.directory)
            os.fsync(self.directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=self.directory)
            except FileNotFoundError:
                pass
