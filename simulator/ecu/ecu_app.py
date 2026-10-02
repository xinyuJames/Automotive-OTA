import base64
import hashlib
import os
from cryptography.hazmat.primitives.asymmetric import ed25519
from can_bus import CanRPC
from firmware_store import FirmwareStore
from trace_logger import TraceLogger

MAX_FIRMWARE = 16 * 1024 * 1024
CAN_IDS = {'engine': 0x100, 'adas': 0x200}


class ECU:
    def __init__(self, store, public_key):
        self.store = store
        self.tracer = TraceLogger('ecu_' + store.ecu_id)
        self.key = public_key
        self.mode = 'IDLE'
        self.buffer = bytearray()
        self.meta = {}

    def handle_rpc(self, method, params):
        params = params or {}
        try:
            if params.get('vehicle_id') != self.store.vehicle_id:
                raise ValueError('Wrong vehicle')
            if method == 'inventory':
                return {'ok': True, **self.store.inventory()}
            if method == 'enter_programming':
                size = params['expected_size']
                if not isinstance(size, int) or not 0 < size <= MAX_FIRMWARE:
                    raise ValueError('Invalid firmware size')
                if not params.get('version') or params.get('update_type') not in ('SOTA', 'FOTA'):
                    raise ValueError('Missing version/update type')
                self.meta, self.buffer, self.mode = params, bytearray(), 'PROGRAMMING'
            elif method == 'write_block':
                if self.mode != 'PROGRAMMING':
                    raise ValueError('Not programming')
                block = base64.b64decode(params['block_b64'], validate=True)
                if params['offset'] != len(self.buffer) or len(self.buffer) + len(block) > self.meta['expected_size']:
                    raise ValueError('Invalid block offset or size')
                self.buffer.extend(block)
            elif method == 'verify':
                if self.mode != 'PROGRAMMING' or len(self.buffer) != self.meta['expected_size']:
                    raise ValueError('Incomplete transfer')
                digest = hashlib.sha256(self.buffer).hexdigest()
                if digest != self.meta['expected_sha256']:
                    raise ValueError('Firmware hash mismatch')
                self.key.verify(base64.b64decode(self.meta['expected_signature']), digest.encode())
                self.store.stage(self.buffer, {'version': self.meta['version'], 'sha256': digest,
                    'size': len(self.buffer), 'update_type': self.meta['update_type'],
                    'campaign_id': self.meta.get('campaign_id')})
                self.mode = 'VERIFIED'
                self.trace('FIRMWARE_VERIFIED')
            elif method == 'activate':
                if self.mode != 'VERIFIED':
                    raise ValueError('Not verified')
                if params.get('simulate_failure'):
                    self.mode = 'IDLE'
                    raise ValueError('Simulated activation failure; current firmware unchanged')
                result = self.store.activate()
                self.mode = 'ACTIVATED'
                self.trace('FIRMWARE_ACTIVATED')
                return {'ok': True, **result}
            elif method == 'confirm':
                if self.mode != 'ACTIVATED':
                    raise ValueError('Not activated')
                result = self.store.inventory()
                self.mode = 'CONFIRMED'
                self.trace('FIRMWARE_CONFIRMED')
                return {'ok': True, **result}
            else:
                raise ValueError('Unknown method')
            return {'ok': True}
        except Exception as error:
            self.trace('RPC_FAILED', method=method, error=str(error) or type(error).__name__)
            return {'ok': False, 'error': str(error) or type(error).__name__}

    def trace(self, event, **details):
        self.tracer.log(event, {'vehicle_id': self.store.vehicle_id, 'ecu_id': self.store.ecu_id,
            'campaign_id': self.meta.get('campaign_id'), 'version': self.meta.get('version'), **details})


def main():
    ecu_id = os.environ['ECU_ID']
    store = FirmwareStore(os.environ['ECU_STORAGE_DIR'], os.environ['VEHICLE_ID'], ecu_id)
    key = ed25519.Ed25519PublicKey.from_public_bytes(base64.b64decode(os.environ['OTA_PUBLIC_KEY']))
    ecu = ECU(store, key)
    can = CanRPC(CAN_IDS[ecu_id])
    while True:
        request = can.receive(CAN_IDS[ecu_id])
        if request:
            params = request.get('p') or {}
            result = ecu.handle_rpc(request.get('m'), params)
            result['request_id'] = params.get('request_id')
            can.send(CAN_IDS[ecu_id] + 1, 'response', result)


if __name__ == '__main__':
    main()
