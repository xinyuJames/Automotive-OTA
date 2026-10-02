import json
import os
import grpc
import ota_pb2 as pb
import ota_pb2_grpc as rpc


class ControlPlaneClient:
    def __init__(self, vehicle_id):
        self.vehicle_id = vehicle_id
        self.channel = grpc.insecure_channel(os.getenv('CONTROL_PLANE_TARGET', 'control-plane:50051'))
        self.stub = rpc.OtaControlStub(self.channel)

    def check_in(self):
        return self.stub.CheckIn(pb.CheckInRequest(vehicle_id=self.vehicle_id), timeout=5).ok

    def get_manifest(self, manifest_ref):
        response = self.stub.GetManifest(pb.GetManifestRequest(manifest_ref=manifest_ref), timeout=10)
        if not response.found:
            raise ValueError('Manifest not found')
        return {'manifest': json.loads(response.manifest_json), 'signature': response.signature}

    def report_status(self, job_id, status, details=None):
        return self.stub.UpdateJobStatus(pb.UpdateJobStatusRequest(job_id=job_id, status=status,
            details=json.dumps(details or {})), timeout=5).received

    def create_or_resume_job(self, campaign_id):
        response = self.stub.CreateJob(pb.CreateJobRequest(vehicle_id=self.vehicle_id, campaign_id=campaign_id), timeout=5)
        if not response.created:
            raise ValueError('Job not created')
        return response.job_id

    def get_vehicle(self):
        return json.loads(self.stub.GetVehicle(pb.VehicleRequest(vehicle_id=self.vehicle_id), timeout=5).json)

    def register_vehicle(self, profile):
        return json.loads(self.stub.RegisterVehicle(pb.VehicleRecord(vehicle_id=self.vehicle_id,
            profile_json=json.dumps(profile)), timeout=5).json)

    def report_inventory(self, ecu_id, inventory):
        return self.stub.ReportInventory(pb.InventoryRecord(vehicle_id=self.vehicle_id, ecu_id=ecu_id,
            inventory_json=json.dumps(inventory)), timeout=5).ok

    def record_event(self, event_id, kind, details):
        return self.stub.RecordVehicleEvent(pb.VehicleEvent(vehicle_id=self.vehicle_id, event_id=event_id,
            kind=kind, details_json=json.dumps(details)), timeout=5).ok

    def pending_jobs(self):
        return json.loads(self.stub.ListPendingJobs(pb.VehicleRequest(vehicle_id=self.vehicle_id), timeout=5).json)

    def confirm_emergency_stop(self, request_id, status='STOPPED'):
        return self.stub.ConfirmEmergencyStop(pb.ConfirmEmergencyStopRequest(
            vehicle_id=self.vehicle_id, request_id=request_id, status=status), timeout=5).acknowledged
