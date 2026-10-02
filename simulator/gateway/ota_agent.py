import os, time, json, logging, threading, base64
import hashlib
import uuid
from pathlib import Path
import bsdiff4
from cryptography.hazmat.primitives.asymmetric import ed25519

from mqtt_client import OTAEventListener
from control_plane_client import ControlPlaneClient
from downloader import ArtifactDownloader
from trace_logger import TraceLogger

TRACER = TraceLogger("gateway")

# Constants
STATES = {
    "IDLE": "IDLE",
    "NOTIFIED": "NOTIFIED",
    "CONFIRMING": "CONFIRMING",
    "WAITING_FOR_APPROVAL": "WAITING_FOR_APPROVAL",
    "DOWNLOADING": "DOWNLOADING",
    "STAGED": "STAGED",
    "INSTALLING": "INSTALLING",
    "VALIDATING": "VALIDATING", 
    "SUCCEEDED": "SUCCEEDED",
    "FAILED": "FAILED",
    "STOPPED": "STOPPED",
    "ROLLED_BACK": "ROLLED_BACK"
}

ECUS = ["engine", "adas"]
CAN_IDS = {
    "engine": {"tx": 0x100, "rx": 0x101},
    "adas":   {"tx": 0x200, "rx": 0x201}
}

class OTAAgent:
    def __init__(self, vehicle_id, can_rpc):
        self.vehicle_id = vehicle_id
        self.can_rpc = can_rpc
        
        # Config
        self.cp_url = os.getenv("CONTROL_PLANE_URL", "http://control-plane:50051")
        self.broker = os.getenv("MQTT_BROKER", "mqtt")
        self.storage_dir = Path(os.environ['GATEWAY_STORAGE_DIR'])
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.storage_path = str(self.storage_dir / 'ota_state.json')
        self.can_lock = threading.Lock()
        self.state_lock = threading.RLock()
        self.inventory = {}
        self.seen_campaigns = []
        self.manifest_ref = None
        self.campaign_candidate = None
        self.approved_digest = None
        self.pending_status = None
        self.last_sync = 0
        self.failure_reason = None
        self.simulate_failure = False
        self.vehicle_state = {'battery_soc': 50, 'gear': 'P', 'parking_brake': True, 'ignition': 'ON', 'speed': 0}
        
        # Clients
        self.cp_client = ControlPlaneClient(vehicle_id)
        self.mqtt_listener = OTAEventListener(self.broker, vehicle_id, 
                                              on_notify=self.on_mqtt_notify,
                                              on_stop=self.on_mqtt_stop)
        self.downloader = ArtifactDownloader()
        
        # State
        self.state = STATES["IDLE"]
        self.job_id = None
        self.campaign_id = None
        self.manifest = None
        self.artifacts_map = {} # Loaded from manifest
        self.progress = {"percent": 0, "status": "Idle"}
        self.user_approved = False
        
        # Crypto
        # Hardcoding the public key for simulation (matches backend)
        # In prod this comes from secure storage or PKI
        self.backend_pub_key = ed25519.Ed25519PublicKey.from_public_bytes(
            base64.b64decode("BI/ezKPNG+EgSFCMkOnowq8sX8ZSnGZyN06cKtUxiss=")
        )
        
        # Load persisted state
        self.load_state()

        # Start Loop
        self.running = True
        self.thread = threading.Thread(target=self.loop)
        self.thread.start()
        
        # Start MQTT
        self.mqtt_listener.start()

    def load_state(self):
        if not os.path.exists(self.storage_path):
            return
        data = json.loads(Path(self.storage_path).read_text())
        if data.get('vehicle_id') != self.vehicle_id:
            raise ValueError('Gateway storage belongs to another vehicle')
        for name in ('state', 'job_id', 'campaign_id', 'manifest_ref', 'campaign_candidate',
                     'approved_digest', 'seen_campaigns', 'pending_status', 'failure_reason', 'simulate_failure', 'vehicle_state'):
            if name in data:
                setattr(self, name, data[name])
        self.progress = {'percent': 100 if self.state == 'SUCCEEDED' else 0, 'status': self.state}
        # Re-fetch and verify signed metadata and bytes after restart; never resume
        # STAGED/INSTALLING from an empty in-memory artifact map.
        if self.state not in ('IDLE', 'SUCCEEDED', 'FAILED', 'STOPPED', 'NOTIFIED'):
            self.state = 'CONFIRMING'

    def save_state(self):
        with self.state_lock:
            data = {name: getattr(self, name) for name in (
                'state', 'job_id', 'campaign_id', 'manifest_ref', 'campaign_candidate',
                'approved_digest', 'seen_campaigns', 'pending_status', 'failure_reason', 'simulate_failure', 'vehicle_state')}
            data['vehicle_id'] = self.vehicle_id
            temporary = self.storage_path + '.part'
            with open(temporary, 'w') as handle:
                json.dump(data, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.storage_path)

    def set_state(self, new_state, details=None):
        logging.info('State Transition: %s -> %s', self.state, new_state)
        TRACER.log('STATE_TRANSITION', {'vehicle_id': self.vehicle_id,
            'campaign_id': self.campaign_id, 'job_id': self.job_id,
            'from': self.state, 'to': new_state, 'details': details})
        self.state = new_state
        self.progress['status'] = new_state
        if details and details.get('error'):
            self.failure_reason = details['error']
        if new_state in ('SUCCEEDED', 'FAILED', 'STOPPED') and self.campaign_id:
            if self.campaign_id not in self.seen_campaigns:
                self.seen_campaigns.append(self.campaign_id)
        if self.job_id:
            self.pending_status = {'job_id': self.job_id, 'status': new_state,
                                   'details': details or {'ecus': self.inventory}}
        self.save_state()
        self.sync_status()

    def sync_status(self):
        if self.pending_status:
            try:
                pending = self.pending_status
                if self.cp_client.report_status(pending['job_id'], pending['status'], pending['details']):
                    self.pending_status = None
                    self.save_state()
            except Exception as error:
                logging.warning('Status retained locally for retry: %s', error)

    def on_mqtt_notify(self, payload):
        with self.state_lock:
            candidate = payload.get('campaign_id')
            if not candidate or candidate in self.seen_campaigns or self.pending_status:
                return
            if self.state in ('IDLE', 'SUCCEEDED', 'FAILED'):
                self.campaign_candidate = candidate
                self.manifest_ref = payload.get('manifest_ref')
                self.approved_digest = None
                self.user_approved = False
                self.simulate_failure = False
                self.artifacts_map = {}
                self.failure_reason = None
                self.progress['percent'] = 0
                self.job_id = None
                self.set_state('NOTIFIED')

    def ecu_call(self, ecu_id, method, params=None):
        request_id = uuid.uuid4().hex
        request = {**(params or {}), 'vehicle_id': self.vehicle_id, 'request_id': request_id}
        target = CAN_IDS[ecu_id]
        with self.can_lock:
            self.can_rpc.send(target['tx'], method, request)
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                reply = self.can_rpc.receive(target['rx'], timeout=max(0.01, deadline - time.monotonic()))
                if not reply:
                    break
                payload = reply.get('p', {})
                if payload.get('request_id') != request_id:
                    continue
                if not payload.get('ok'):
                    raise ValueError(f"{ecu_id} {method}: {payload.get('error', 'Rejected')}")
                return {key: value for key, value in payload.items() if key not in ('ok', 'request_id')}
        raise TimeoutError(f'{ecu_id} {method}: no matching response')

    def refresh_inventory(self):
        for ecu_id in ECUS:
            observed = self.ecu_call(ecu_id, 'inventory')
            self.inventory[ecu_id] = observed
            self.cp_client.report_inventory(ecu_id, observed)

    def on_mqtt_stop(self, payload):
        logging.warning("Received Emergency Stop Signal!")
        # Pause everything
        prev_state = self.state
        
        # Confirm with CP
        try:
            policy = self.cp_client.confirm_emergency_stop(payload.get("stop_scope"), payload.get("nonce"))
            if isinstance(policy, dict) and policy.get("active"):
                self.set_state(STATES["STOPPED"])
            else:
                logging.info("Emergency Stop not confirmed by CP. Ignoring.")
        except Exception as e:
            logging.error(f"Failed to confirm stop: {e}")

    def set_vehicle_state(self, new_state):
        # Update internal state simulation
        if not hasattr(self, 'vehicle_state'):
             self.vehicle_state = {
                 "battery_soc": 50,
                 "gear": "P",
                 "parking_brake": True,
                 "ignition": "ON",
                 "speed": 0
             }
        allowed = {'battery_soc', 'gear', 'parking_brake', 'ignition', 'speed'}
        if set(new_state) - allowed:
            raise ValueError('Unknown vehicle condition')
        proposed = {**self.vehicle_state, **new_state}
        if (not isinstance(proposed['battery_soc'], (int, float)) or not 0 <= proposed['battery_soc'] <= 100
                or proposed['gear'] not in ('P', 'R', 'N', 'D') or proposed['ignition'] not in ('OFF', 'ACC', 'ON', 'RUN')
                or not isinstance(proposed['parking_brake'], bool) or not isinstance(proposed['speed'], (int, float))
                or not 0 <= proposed['speed'] <= 400):
            raise ValueError('Invalid vehicle conditions')
        self.vehicle_state.update(new_state)
        self.save_state()
        logging.info(f"Vehicle State Updated: {self.vehicle_state}")
        
    def check_preconditions(self):
        if not self.manifest: return False, "No Manifest"
        policy = self.manifest.get("policy", {})
        
        # Initialize defaults if not set
        if not hasattr(self, 'vehicle_state'):
             self.set_vehicle_state({})

        vs = self.vehicle_state
        
        # Check Battery
        min_soc = policy.get("min_battery_soc", 0)
        if vs["battery_soc"] < min_soc:
            return False, f"Battery too low ({vs['battery_soc']}% < {min_soc}%)"
            
        # Check Gear
        req_gear = policy.get("required_gear")
        if req_gear and vs["gear"] != req_gear:
            return False, f"Gear must be in {req_gear} (Current: {vs['gear']})"
            
        # Check Brake
        if policy.get("requires_parking_brake") and not vs["parking_brake"]:
             return False, "Parking Brake must be engaged"
             
        # Check Ignition
        req_ign = policy.get("requires_ignition_state")
        if req_ign and vs["ignition"] != req_ign:
             return False, f"Ignition must be {req_ign} (Current: {vs['ignition']})"
             
        if policy.get('requires_parked') and vs.get('speed', 0) != 0:
            return False, 'Vehicle must be stationary'
        return True, "OK"

    def approve_update(self, simulate_failure=False):
        if self.state == STATES["WAITING_FOR_APPROVAL"]:
            # Check Preconditions
            ok, reason = self.check_preconditions()
            if not ok:
                logging.warning(f"Approval Blocked: {reason}")
                return False, reason

            digest = hashlib.sha256(json.dumps(self.manifest, sort_keys=True).encode()).hexdigest()
            try:
                self.cp_client.record_event(str(uuid.uuid4()), 'approval',
                    {'job_id': self.job_id, 'manifest_sha256': digest, 'approved': True})
            except Exception as error:
                return False, f'Could not persist approval: {error}'
            self.approved_digest = digest
            self.simulate_failure = simulate_failure
            self.user_approved = True
            self.save_state()
            logging.info("User approved update.")
            return True, "OK"
        return False, "Not in waiting state"

    def loop(self):
        while self.running:
            time.sleep(1)
            try:
                self.sync_status()
                if time.monotonic() - self.last_sync > 10:
                    self.last_sync = time.monotonic()
                    try:
                        self.refresh_inventory()
                        self.cp_client.check_in()
                        if self.state in ('IDLE', 'SUCCEEDED', 'FAILED'):
                            for job in self.cp_client.pending_jobs():
                                self.on_mqtt_notify(job)
                                if self.state == 'NOTIFIED':
                                    break
                    except Exception as error:
                        logging.warning('Inventory/discovery will retry: %s', error)
                if self.state == STATES["NOTIFIED"]:
                    self.handle_notified()
                elif self.state == STATES["CONFIRMING"]:
                    self.handle_confirming()
                elif self.state == STATES["WAITING_FOR_APPROVAL"]:
                    if self.user_approved:
                        self.set_state(STATES["DOWNLOADING"])
                elif self.state == STATES["DOWNLOADING"]:
                    self.handle_downloading()
                elif self.state == STATES["STAGED"]:
                    self.handle_installing() # Auto proceed to install for sim simplicity, or check local gates
                elif self.state == STATES["INSTALLING"]:
                    pass # Handled in handle_installing which blocks or runs async
                elif self.state == STATES["VALIDATING"]:
                    pass
            except Exception as e:
                logging.error(f"Error in Agent Loop: {e}")
                self.set_state(STATES['FAILED'], {'error': str(e)})

    def handle_notified(self):
        # Call CP to Confirm Eligibility and Get Job
        try:
            self.job_id = self.cp_client.create_or_resume_job(self.campaign_candidate)
            self.campaign_id = self.campaign_candidate
            self.set_state(STATES["CONFIRMING"])
        except Exception as e:
            logging.error(f"Failed to create job: {e}")
            self.set_state(STATES["IDLE"])

    def handle_confirming(self):
        # Get Manifest
        try:
            if not self.manifest_ref:
                raise ValueError('Missing manifest reference')
            data = self.cp_client.get_manifest(self.manifest_ref)
            
            # Verify Signature
            manifest = data["manifest"]
            signature = base64.b64decode(data["signature"])
            manifest_bytes = json.dumps(manifest, sort_keys=True).encode()

            # Signed metadata must also identify this vehicle and execution.
            
            try:
                self.backend_pub_key.verify(signature, manifest_bytes)
                logging.info("Manifest Signature Verified.")
            except Exception:
                logging.error("Manifest Signature Verification FAILED!")
                self.set_state(STATES["FAILED"], {"error": "Bad Manifest Sig"})
                return

            if manifest.get('vehicle_id') != self.vehicle_id or manifest.get('campaign_id') != self.campaign_id:
                raise ValueError('Manifest vehicle/campaign mismatch')
            if manifest.get('manifest_ref') != self.manifest_ref or manifest.get('expires_at', 0) <= time.time():
                raise ValueError('Manifest reference invalid or manifest expired')
            targets = manifest.get('targets', [])
            ecu_ids = [target['ecu_id'] for target in targets]
            if not targets or len(set(ecu_ids)) != len(ecu_ids) or any(ecu not in ECUS for ecu in ecu_ids):
                raise ValueError('Invalid manifest ECU targets')
            self.manifest = manifest
            digest = hashlib.sha256(manifest_bytes).hexdigest()
            self.user_approved = self.approved_digest == digest
            self.set_state(STATES["WAITING_FOR_APPROVAL"])
        except Exception as e:
             logging.error(f"Failed to get/verify manifest: {e}")
             self.set_state(STATES["FAILED"], {'error': str(e)})

    def handle_downloading(self):
        self.artifacts_map = {}
        for target in self.manifest['targets']:
            ecu = target['ecu_id']
            # Content hashes name cache files; campaign/version strings never form paths.
            directory = self.storage_dir / 'downloads' / ecu
            local_path = directory / (target['download_sha256'] + '.artifact')
            if len(target['download_sha256']) != 64 or any(c not in '0123456789abcdef' for c in target['download_sha256']):
                raise ValueError('Invalid download digest')
            if not self.downloader.download(target['artifact_url'], local_path,
                                             target['download_sha256'], target['download_size']):
                raise ValueError(f'{ecu}: download integrity/size failure')
            content = local_path.read_bytes()
            size = target['target_size']
            if not isinstance(size, int) or not 0 < size <= 16 * 1024 * 1024:
                raise ValueError('Invalid target size')
            observed = self.ecu_call(ecu, 'inventory')
            if observed.get('sha256') == target['target_sha256'] and observed.get('current_version') == target['target_version']:
                # Recovery is resolved while preparing artifacts, before installation.
                self.inventory[ecu] = observed
                self.artifacts_map[ecu] = None
                continue
            if target['artifact_type'] == 'delta':
                if observed.get('sha256') != target['base_sha256'] or observed.get('current_version') != target['base_version']:
                    raise ValueError(f'{ecu}: installed base does not match delta')
                base_path = directory / 'base.bin'
                if not self.downloader.download(target['base_url'], base_path, target['base_sha256'], target['base_size']):
                    raise ValueError('Base download failed')
                # BSDIFF40 encodes output size in its header. Bound it before allocation.
                if len(content) < 32 or content[:8] != b'BSDIFF40' or int.from_bytes(content[24:32], 'little') != size:
                    raise ValueError('Invalid or oversized delta output')
                content = bsdiff4.patch(base_path.read_bytes(), content)
            elif target['artifact_type'] != 'full':
                raise ValueError('Unsupported artifact type')
            if len(content) != size or hashlib.sha256(content).hexdigest() != target['target_sha256']:
                raise ValueError('Final firmware hash/size mismatch')
            (directory / 'verified.bin').write_bytes(content)
            self.artifacts_map[ecu] = content
        self.progress['percent'] = 50
        self.set_state('STAGED')

    def handle_installing(self):
        targets = self.manifest['targets']
        if set(self.artifacts_map) != {target['ecu_id'] for target in targets}:
            raise ValueError('Incomplete verified artifacts')
        ok, reason = self.check_preconditions()
        if not ok:
            raise ValueError(reason)
        self.set_state('INSTALLING')
        for target in sorted(targets, key=lambda value: value.get('install_order', 0)):
            ok, reason = self.check_preconditions()
            if not ok:
                raise ValueError(reason)
            ecu = target['ecu_id']
            firmware = self.artifacts_map[ecu]
            if firmware is not None:  # None means already installed during restart recovery.
                self.inventory[ecu] = self.flash_ecu(ecu, firmware)
        self.progress['percent'] = 100
        self.set_state('SUCCEEDED', {'ecus': self.inventory})
        # If cloud reporting is temporarily unavailable, periodic inventory sync retries.
        try:
            self.refresh_inventory()
        except Exception as error:
            logging.warning('Installed inventory reporting will retry: %s', error)

    def flash_ecu(self, ecu_id, firmware_data):
        target = next(t for t in self.manifest['targets'] if t['ecu_id'] == ecu_id)
        if firmware_data is None:
            raise ValueError('Firmware bytes missing')
        self.ecu_call(ecu_id, 'enter_programming', {
            'expected_size': target['target_size'], 'expected_sha256': target['target_sha256'],
            'expected_signature': target['artifact_signature'], 'version': target['target_version'],
            'update_type': target['update_type'], 'campaign_id': self.campaign_id})
        for offset in range(0, len(firmware_data), 512):
            if self.state == 'STOPPED':
                raise ValueError('Installation interrupted')
            self.ecu_call(ecu_id, 'write_block', {'offset': offset,
                'block_b64': base64.b64encode(firmware_data[offset:offset + 512]).decode()})
        self.ecu_call(ecu_id, 'verify')
        self.ecu_call(ecu_id, 'activate', {'simulate_failure': getattr(self, 'simulate_failure', False)})
        return self.ecu_call(ecu_id, 'confirm')
