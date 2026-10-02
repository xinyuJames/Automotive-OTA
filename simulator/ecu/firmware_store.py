"""Local ECU storage interface. Only this ECU mounts and writes its directory."""
import hashlib
import json
import os
from pathlib import Path
import time
import uuid


class FirmwareStore:
    def __init__(self, root, vehicle_id, ecu_id):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.vehicle_id, self.ecu_id = vehicle_id, ecu_id
        identity = self.root / 'identity.json'
        expected = {'vehicle_id': vehicle_id, 'ecu_id': ecu_id}
        if identity.exists() and json.loads(identity.read_text()) != expected:
            raise ValueError('ECU storage belongs to a different vehicle or ECU')
        self.write_json(identity, expected)

    @staticmethod
    def write_json(path, data):
        temporary = path.with_name(path.name + '.part')
        with temporary.open('w') as handle:
            json.dump(data, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)

    def pointer(self, name, destination):
        temporary = self.root / ('.' + name + '-' + uuid.uuid4().hex)
        temporary.symlink_to(destination)
        os.replace(temporary, self.root / name)
        directory = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def inventory(self):
        result = {'vehicle_id': self.vehicle_id, 'ecu_id': self.ecu_id,
                  'current_version': None, 'previous_version': None, 'sha256': None}
        for name in ('current', 'previous'):
            folder = self.root / name
            if folder.exists():
                metadata = json.loads((folder / 'metadata.json').read_text())
                content = (folder / 'firmware.bin').read_bytes()
                digest = hashlib.sha256(content).hexdigest()
                if digest != metadata['sha256'] or len(content) != metadata['size']:
                    raise ValueError(f'{name} firmware failed read-back integrity check')
                result[name + '_version'] = metadata['version']
                if name == 'current':
                    result.update(sha256=digest, size=len(content), update_type=metadata['update_type'],
                                  campaign_id=metadata.get('campaign_id'), installed_at=metadata.get('installed_at'))
        return result

    def stage(self, content, metadata):
        staged = self.root / 'staged'
        staged.mkdir(exist_ok=True)
        with (staged / 'firmware.bin').open('wb') as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        self.write_json(staged / 'metadata.json', {**metadata, 'installed_at': time.time()})

    def activate(self):
        staged = self.root / 'staged'
        metadata = json.loads((staged / 'metadata.json').read_text())
        content = (staged / 'firmware.bin').read_bytes()
        if hashlib.sha256(content).hexdigest() != metadata['sha256'] or len(content) != metadata['size']:
            raise ValueError('Staged firmware integrity mismatch')
        version_dir = self.root / 'versions' / (metadata['sha256'] + '-' + uuid.uuid4().hex[:8])
        version_dir.parent.mkdir(exist_ok=True)
        os.replace(staged, version_dir)
        if (self.root / 'current').is_symlink():
            self.pointer('previous', os.readlink(self.root / 'current'))
        self.pointer('current', str(version_dir.relative_to(self.root)))
        return self.inventory()
