"""Build signed campaigns from catalog firmware through network APIs only."""
import base64
import hashlib
import json
import os
import time
import uuid

import bsdiff4
import grpc
import paho.mqtt.client as mqtt
from cryptography.hazmat.primitives.asymmetric import ed25519
import ota_pb2 as pb
import ota_pb2_grpc as rpc
from trace_logger import TraceLogger

VEHICLE_ID = os.getenv('VEHICLE_ID', 'VIN_SIM_0001')
OPTIONS = [('grpc.max_receive_message_length', 17 * 1024 * 1024),
           ('grpc.max_send_message_length', 17 * 1024 * 1024)]
TRACER = TraceLogger('backend')

class Orchestrator:
    def __init__(self):
        self.key = ed25519.Ed25519PrivateKey.from_private_bytes(base64.b64decode(os.environ['OTA_SECRET_KEY']))
        self.store_channel = grpc.insecure_channel(os.getenv('ARTIFACT_GRPC_TARGET', 'artifact-server:50052'), options=OPTIONS)
        self.cp_channel = grpc.insecure_channel(os.getenv('CONTROL_PLANE_TARGET', 'control-plane:50051'))
        self.store = rpc.ArtifactStoreStub(self.store_channel)
        self.cp = rpc.OtaControlStub(self.cp_channel)
        self.last_offer = None

    def sign(self, content):
        return base64.b64encode(self.key.sign(content)).decode()

    def setup_artifacts(self, vehicle):
        catalog = json.loads(self.store.ListReleases(pb.Empty(), timeout=10).json)
        targets = []
        for ecu, component in catalog['ecus'].items():
            version = component['latest']
            observed = vehicle.get('ecus', {}).get(ecu, {})
            installed = observed.get('current_version')
            if not version:
                continue
            if installed and installed.isascii() and installed.isalpha() and (len(installed), installed.upper()) > (len(version), version):
                continue  # Removing a newer source file must not downgrade this ECU.
            response = self.store.ReadFirmware(pb.FirmwareRequest(ecu_id=ecu, version=version), timeout=30)
            metadata = json.loads(response.metadata_json)
            target_hash = hashlib.sha256(response.content).hexdigest()
            if metadata['sha256'] != target_hash or metadata['size'] != len(response.content):
                raise ValueError('Catalog firmware integrity mismatch')
            if observed.get('sha256') == target_hash and installed == version:
                continue
            download = metadata
            artifact_type = 'full'
            base = None
            base_version = observed.get('current_version')
            if base_version in component['versions']:
                source = self.store.ReadFirmware(pb.FirmwareRequest(ecu_id=ecu, version=base_version), timeout=30)
                base = json.loads(source.metadata_json)
                if hashlib.sha256(source.content).hexdigest() == observed.get('sha256') == base['sha256']:
                    patch = bsdiff4.diff(source.content, response.content)
                    download = json.loads(self.store.PutArtifact(pb.ArtifactData(
                        content=patch, sha256=hashlib.sha256(patch).hexdigest()), timeout=30).json)
                    artifact_type = 'delta'
                else:
                    base = None
            target = {
                'ecu_id': ecu, 'component_name': component['component_name'],
                'target_version': version, 'base_version': base_version,
                'update_type': component['update_type'], 'release_notes': metadata.get('release_notes', ''),
                'artifact_type': artifact_type, 'artifact_url': download['url'],
                'download_sha256': download['sha256'], 'download_size': download['size'],
                'target_sha256': target_hash, 'target_size': len(response.content),
                'artifact_signature': self.sign(target_hash.encode()), 'install_order': len(targets) + 1,
            }
            if artifact_type == 'delta':
                target.update(base_sha256=base['sha256'], base_size=base['size'], base_url=base['url'])
            targets.append(target)
        return targets

    def launch(self):
        grpc.channel_ready_future(self.store_channel).result(timeout=10)
        grpc.channel_ready_future(self.cp_channel).result(timeout=10)
        # Do not overwrite registration or create duplicate unfinished campaigns on restart.
        pending = json.loads(self.cp.ListPendingJobs(pb.VehicleRequest(vehicle_id=VEHICLE_ID), timeout=10).json)
        if pending:
            return pending[0]['campaign_id'], pending[0]['manifest_ref']
        vehicle = json.loads(self.cp.GetVehicle(pb.VehicleRequest(vehicle_id=VEHICLE_ID), timeout=10).json)
        targets = self.setup_artifacts(vehicle)
        if not targets:
            return None
        offer = json.dumps(targets, sort_keys=True)
        if offer == self.last_offer:
            return None
        campaign = 'cam-' + uuid.uuid4().hex[:12]
        manifest = {
            'schema_version': '1.1', 'vehicle_id': VEHICLE_ID, 'campaign_id': campaign,
            'manifest_ref': 'manifest-' + campaign, 'created_at': time.time(),
            'expires_at': time.time() + 86400, 'targets': targets,
            'policy': {'requires_driver_approval': True, 'requires_parked': True,
                       'min_battery_soc': 20, 'required_gear': 'P',
                       'requires_parking_brake': True, 'requires_ignition_state': 'ON'},
        }
        document = json.dumps(manifest, sort_keys=True)
        result = self.cp.RegisterManifest(pb.RegisterManifestRequest(
            manifest_json=document, signature=self.sign(document.encode()),
            manifest_ref=manifest['manifest_ref']), timeout=10)
        if not result.success:
            raise RuntimeError(result.message)
        self.last_offer = offer
        TRACER.log('MANIFEST_REGISTERED', {'vehicle_id': VEHICLE_ID, 'campaign_id': campaign})
        return campaign, manifest['manifest_ref']


def monitor_updates(orchestrator, client):
    while True:
        try:
            campaign = orchestrator.launch()
            if campaign:
                client.publish(f'v1/vehicles/{VEHICLE_ID}/ota/notify', json.dumps({
                    'campaign_id': campaign[0], 'manifest_ref': campaign[1],
                }), qos=1)
        except Exception as error:
            print(f'Update check failed; retrying in 10 seconds: {error}', flush=True)
        time.sleep(10)


def main():
    orchestrator = Orchestrator()
    client = mqtt.Client()
    client.connect_async(os.getenv('MQTT_BROKER', 'mqtt'), 1883, 60)
    client.loop_start()
    print('Backend ready. Checking for updates every 10 seconds.', flush=True)
    try:
        monitor_updates(orchestrator, client)
    finally:
        client.loop_stop()


if __name__ == '__main__':
    main()
