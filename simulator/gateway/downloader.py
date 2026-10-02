import hashlib
import logging
import os
from pathlib import Path
import time
import requests

MAX_ARTIFACT = 16 * 1024 * 1024


class ArtifactDownloader:
    def download(self, url, target_path, expected_hash, expected_size):
        if not isinstance(expected_size, int) or not 0 < expected_size <= MAX_ARTIFACT or not expected_hash:
            return False
        path = Path(target_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size == expected_size and self.verify_hash(path, expected_hash):
            return True
        temporary = path.with_name(path.name + '.part')
        try:
            digest, received, start = hashlib.sha256(), 0, time.monotonic()
            with requests.get(url, stream=True, timeout=(5, 15)) as response:
                response.raise_for_status()
                with temporary.open('wb') as handle:
                    for block in response.iter_content(65536):
                        received += len(block)
                        if received > expected_size or time.monotonic() - start > 120:
                            raise ValueError('Download exceeds size or time limit')
                        digest.update(block)
                        handle.write(block)
                    handle.flush()
                    os.fsync(handle.fileno())
            if received != expected_size or digest.hexdigest() != expected_hash:
                raise ValueError('Downloaded size/hash mismatch')
            os.replace(temporary, path)
            return True
        except Exception as error:
            logging.error('Download failed: %s', error)
            temporary.unlink(missing_ok=True)
            return False

    def verify_hash(self, path, expected_hash):
        digest = hashlib.sha256()
        with open(path, 'rb') as handle:
            for block in iter(lambda: handle.read(65536), b''):
                digest.update(block)
        return digest.hexdigest() == expected_hash
