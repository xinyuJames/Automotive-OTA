"""Owns release files and fleet records. Clients use gRPC; downloads use HTTP."""
import hashlib
import json
import os
import re
from pathlib import Path
import sqlite3
import time
import uuid
from concurrent import futures
from contextlib import contextmanager
from functools import wraps
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import grpc
import ota_pb2 as pb
import ota_pb2_grpc as rpc

MAX_ARTIFACT = 16 * 1024 * 1024
OPTIONS = [('grpc.max_receive_message_length', MAX_ARTIFACT + 65536),
           ('grpc.max_send_message_length', MAX_ARTIFACT + 65536)]


def checked(fn):
    @wraps(fn)
    def call(self, request, context):
        try:
            return fn(self, request, context)
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(error))
        except FileNotFoundError as error:
            context.abort(grpc.StatusCode.NOT_FOUND, str(error))
    return call


def object_json(value):
    result = json.loads(value)
    if not isinstance(result, dict):
        raise ValueError('Expected a JSON object')
    return result


class Store(rpc.ArtifactStoreServicer):
    def __init__(self, root, public_url):
        self.root = Path(root)
        self.public_url = public_url.rstrip('/')
        self.blobs = self.root / 'data' / 'blobs'
        self.blobs.mkdir(parents=True, exist_ok=True)
        self.database = self.root / 'data' / 'registry.sqlite3'
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS releases (
                    ecu_id TEXT, version TEXT, sha256 TEXT NOT NULL,
                    PRIMARY KEY(ecu_id, version));
                CREATE TABLE IF NOT EXISTS vehicles (
                    vehicle_id TEXT PRIMARY KEY, profile TEXT NOT NULL, last_seen REAL);
                CREATE TABLE IF NOT EXISTS inventory (
                    vehicle_id TEXT, ecu_id TEXT, record TEXT NOT NULL, observed_at REAL,
                    PRIMARY KEY(vehicle_id, ecu_id));
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY, vehicle_id TEXT, kind TEXT, details TEXT, recorded_at REAL);
                CREATE TABLE IF NOT EXISTS manifests (
                    manifest_ref TEXT PRIMARY KEY, document TEXT NOT NULL, signature TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY, vehicle_id TEXT, campaign_id TEXT, status TEXT,
                    details TEXT, created_at REAL, updated_at REAL, UNIQUE(vehicle_id, campaign_id));
            ''')
        for path in (self.root / 'registrations').glob('*.json'):
            profile = object_json(path.read_text())
            vehicle_id = profile.pop('vehicle_id')
            with self.connect() as db:
                db.execute('INSERT OR IGNORE INTO vehicles VALUES (?, ?, NULL)',
                           (vehicle_id, json.dumps(profile)))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def require_vehicle(self, db, vehicle_id):
        if not db.execute('SELECT 1 FROM vehicles WHERE vehicle_id=?', (vehicle_id,)).fetchone():
            raise ValueError('Vehicle must be registered first')

    def put_blob(self, content):
        if not content or len(content) > MAX_ARTIFACT:
            raise ValueError('Artifact must contain 1..16777216 bytes')
        digest = hashlib.sha256(content).hexdigest()
        path = self.blobs / digest
        if not path.exists():
            temporary = path.with_name(digest + '.' + uuid.uuid4().hex + '.part')
            with temporary.open('wb') as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        return {'sha256': digest, 'size': len(content), 'url': f'{self.public_url}/blobs/{digest}'}

    def release_catalog(self):
        catalog = json.loads((self.root / 'catalog.json').read_text())
        for ecu, component in catalog['ecus'].items():
            descriptions = component.get('versions', {})
            versions = {}
            for path in (self.root / f'{ecu}_firmware').glob('version_*'):
                match = re.fullmatch(r'version_([a-z]+)', path.name)
                if match and path.is_file():
                    version = match[1].upper()
                    versions[version] = {**descriptions.get(version, {}),
                                         'file': str(path.relative_to(self.root))}
            component['versions'] = versions
            # A ... Z, AA ... AZ, BA ...; do not use modification times.
            component['latest'] = max(versions, key=lambda v: (len(v), v), default=None)
        return catalog

    @checked
    def ListReleases(self, request, context):
        return pb.JsonRecord(json=json.dumps(self.release_catalog()))

    @checked
    def ReadFirmware(self, request, context):
        component = self.release_catalog()['ecus'][request.ecu_id]
        release = component['versions'][request.version]
        path = (self.root / release['file']).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ValueError('Firmware path escapes the catalog root')
        if path.stat().st_size > MAX_ARTIFACT:
            raise ValueError('Firmware exceeds size limit')
        content = path.read_bytes()
        metadata = {**release, **self.put_blob(content), 'version': request.version,
                    'ecu_id': request.ecu_id, 'update_type': component['update_type']}
        metadata.pop('file', None)
        with self.connect() as db:
            previous = db.execute('SELECT sha256 FROM releases WHERE ecu_id=? AND version=?',
                                  (request.ecu_id, request.version)).fetchone()
            if previous and previous['sha256'] != metadata['sha256']:
                raise ValueError('Published version content changed; add a new version_<letters> file')
            db.execute('INSERT OR IGNORE INTO releases VALUES (?, ?, ?)',
                       (request.ecu_id, request.version, metadata['sha256']))
        return pb.FirmwareData(content=content, metadata_json=json.dumps(metadata))

    @checked
    def PutArtifact(self, request, context):
        if hashlib.sha256(request.content).hexdigest() != request.sha256:
            raise ValueError('Artifact hash mismatch')
        return pb.JsonRecord(json=json.dumps(self.put_blob(request.content)))

    @checked
    def RegisterVehicle(self, request, context):
        if not request.vehicle_id or len(request.vehicle_id) > 128:
            raise ValueError('Invalid vehicle ID')
        profile = object_json(request.profile_json)
        allowed = {'make', 'model', 'vehicle_type', 'model_year', 'features'}
        if set(profile) - allowed:
            raise ValueError('Unknown registration fields')
        if not isinstance(profile.get('features', []), list) or not all(isinstance(x, str) for x in profile.get('features', [])):
            raise ValueError('features must be a list of strings')
        for key in ('make', 'model', 'vehicle_type', 'model_year'):
            if key in profile and not isinstance(profile[key], str):
                raise ValueError(f'{key} must be a string')
        with self.connect() as db:
            row = db.execute('SELECT profile FROM vehicles WHERE vehicle_id=?', (request.vehicle_id,)).fetchone()
            merged = {**(json.loads(row['profile']) if row else {}), **profile}
            db.execute('INSERT INTO vehicles VALUES (?, ?, NULL) ON CONFLICT(vehicle_id) DO UPDATE SET profile=excluded.profile',
                       (request.vehicle_id, json.dumps(merged)))
        return self.GetVehicle(pb.VehicleRequest(vehicle_id=request.vehicle_id), context)

    @checked
    def GetVehicle(self, request, context):
        with self.connect() as db:
            self.require_vehicle(db, request.vehicle_id)
            row = db.execute('SELECT * FROM vehicles WHERE vehicle_id=?', (request.vehicle_id,)).fetchone()
            result = {'vehicle_id': request.vehicle_id, 'profile': json.loads(row['profile']), 'last_seen': row['last_seen']}
            result['ecus'] = {r['ecu_id']: {**json.loads(r['record']), 'observed_at': r['observed_at']}
                              for r in db.execute('SELECT * FROM inventory WHERE vehicle_id=?', (request.vehicle_id,))}
            result['jobs'] = [dict(r) for r in db.execute('SELECT * FROM jobs WHERE vehicle_id=? ORDER BY created_at DESC LIMIT 100', (request.vehicle_id,))]
            result['events'] = [{**dict(r), 'details': json.loads(r['details'])} for r in db.execute(
                'SELECT * FROM events WHERE vehicle_id=? ORDER BY recorded_at DESC LIMIT 100', (request.vehicle_id,))]
        return pb.JsonRecord(json=json.dumps(result))

    @checked
    def ReportInventory(self, request, context):
        record = object_json(request.inventory_json)
        if request.ecu_id not in ('engine', 'adas'):
            raise ValueError('Unknown ECU')
        if record.get('vehicle_id') != request.vehicle_id or record.get('ecu_id') != request.ecu_id:
            raise ValueError('Inventory identity mismatch')
        if record.get('current_version') and not record.get('sha256'):
            raise ValueError('Installed inventory requires a measured hash')
        with self.connect() as db:
            self.require_vehicle(db, request.vehicle_id)
            old = db.execute('SELECT record FROM inventory WHERE vehicle_id=? AND ecu_id=?', (request.vehicle_id, request.ecu_id)).fetchone()
            encoded = json.dumps(record, sort_keys=True)
            db.execute('INSERT INTO inventory VALUES (?, ?, ?, ?) ON CONFLICT(vehicle_id,ecu_id) DO UPDATE SET record=excluded.record, observed_at=excluded.observed_at',
                       (request.vehicle_id, request.ecu_id, encoded, time.time()))
            if not old or old['record'] != encoded:
                db.execute('INSERT INTO events VALUES (?, ?, ?, ?, ?)',
                           (uuid.uuid4().hex, request.vehicle_id, 'inventory', encoded, time.time()))
        return pb.CheckInResponse(ok=True)

    @checked
    def RecordVehicleEvent(self, request, context):
        details = object_json(request.details_json)
        if request.kind not in ('crash', 'diagnostic', 'approval') or not request.event_id:
            raise ValueError('Event needs a unique ID and a supported kind')
        with self.connect() as db:
            self.require_vehicle(db, request.vehicle_id)
            db.execute('INSERT OR IGNORE INTO events VALUES (?, ?, ?, ?, ?)',
                       (request.event_id, request.vehicle_id, request.kind, json.dumps(details), time.time()))
        return pb.CheckInResponse(ok=True)

    @checked
    def CheckIn(self, request, context):
        with self.connect() as db:
            self.require_vehicle(db, request.vehicle_id)
            db.execute('UPDATE vehicles SET last_seen=? WHERE vehicle_id=?', (time.time(), request.vehicle_id))
        return pb.CheckInResponse(ok=True)

    @checked
    def RegisterManifest(self, request, context):
        manifest = object_json(request.manifest_json)
        if manifest['manifest_ref'] != request.manifest_ref or not manifest.get('targets'):
            raise ValueError('Invalid manifest identity or targets')
        with self.connect() as db:
            self.require_vehicle(db, manifest['vehicle_id'])
            old = db.execute('SELECT * FROM manifests WHERE manifest_ref=?', (request.manifest_ref,)).fetchone()
            if old and (old['document'] != request.manifest_json or old['signature'] != request.signature):
                raise ValueError('Manifest reference already exists with different content')
            db.execute('INSERT OR IGNORE INTO manifests VALUES (?, ?, ?)',
                       (request.manifest_ref, request.manifest_json, request.signature))
            now = time.time()
            db.execute('INSERT OR IGNORE INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?)',
                       ('job-' + uuid.uuid4().hex, manifest['vehicle_id'], manifest['campaign_id'], 'PENDING', '{}', now, now))
        return pb.RegisterManifestResponse(success=True, message='Persisted')

    @checked
    def GetManifest(self, request, context):
        with self.connect() as db:
            row = db.execute('SELECT * FROM manifests WHERE manifest_ref=?', (request.manifest_ref,)).fetchone()
        return pb.GetManifestResponse(found=bool(row), manifest_json=row['document'] if row else '', signature=row['signature'] if row else '')

    @checked
    def CreateJob(self, request, context):
        with self.connect() as db:
            row = db.execute('SELECT * FROM jobs WHERE vehicle_id=? AND campaign_id=?', (request.vehicle_id, request.campaign_id)).fetchone()
        if not row:
            raise ValueError('No registered campaign for this vehicle')
        return pb.CreateJobResponse(job_id=row['job_id'], created=True)

    @checked
    def UpdateJobStatus(self, request, context):
        with self.connect() as db:
            row = db.execute('SELECT * FROM jobs WHERE job_id=?', (request.job_id,)).fetchone()
            if not row:
                raise ValueError('Unknown job')
            db.execute('UPDATE jobs SET status=?, details=?, updated_at=? WHERE job_id=?',
                       (request.status, request.details, time.time(), request.job_id))
            if row['status'] != request.status or row['details'] != request.details:
                db.execute('INSERT INTO events VALUES (?, ?, ?, ?, ?)', (uuid.uuid4().hex, row['vehicle_id'], 'update',
                           json.dumps({'job_id': request.job_id, 'status': request.status, 'details': request.details}), time.time()))
        return pb.UpdateJobStatusResponse(received=True)

    @checked
    def ListPendingJobs(self, request, context):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM jobs WHERE vehicle_id=? AND status NOT IN ('SUCCEEDED','FAILED','STOPPED','ROLLED_BACK') ORDER BY created_at", (request.vehicle_id,)).fetchall()
            manifests = {json.loads(r['document'])['campaign_id']: r['manifest_ref'] for r in db.execute('SELECT * FROM manifests') if json.loads(r['document']).get('vehicle_id') == request.vehicle_id}
        return pb.JsonRecord(json=json.dumps([{**dict(r), 'manifest_ref': manifests[r['campaign_id']]} for r in rows]))


def serve():
    store = Store(os.environ['ARTIFACT_ROOT'], os.getenv('ARTIFACT_PUBLIC_URL', 'http://artifact-server:8082'))

    class DownloadHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            name = self.path.removeprefix('/blobs/')
            if not self.path.startswith('/blobs/') or len(name) != 64 or any(c not in '0123456789abcdef' for c in name):
                self.send_error(404)
                return
            path = store.blobs / name
            if not path.is_file():
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Length', str(path.stat().st_size))
            self.end_headers()
            with path.open('rb') as handle:
                while chunk := handle.read(65536):
                    self.wfile.write(chunk)

    http = ThreadingHTTPServer(('0.0.0.0', 8082), DownloadHandler)
    Thread(target=http.serve_forever, daemon=True).start()
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10), options=OPTIONS)
    rpc.add_ArtifactStoreServicer_to_server(store, server)
    server.add_insecure_port('[::]:50052')
    server.start()
    print('Artifact store: gRPC :50052, HTTP :8082', flush=True)
    server.wait_for_termination()


if __name__ == '__main__':
    serve()
