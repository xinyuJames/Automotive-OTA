"""Control-plane API; persistent records are owned by the artifact registry."""
import os
from concurrent import futures
import grpc
import ota_pb2 as pb
import ota_pb2_grpc as rpc


class OtaControlServicer(rpc.OtaControlServicer):
    def __init__(self):
        self.channel = grpc.insecure_channel(os.getenv('ARTIFACT_GRPC_TARGET', 'artifact-server:50052'))
        self.store = rpc.ArtifactStoreStub(self.channel)

    def ConfirmEmergencyStop(self, request, context):
        context.abort(grpc.StatusCode.UNIMPLEMENTED, 'Emergency stop is not implemented')


def forward(name):
    def method(self, request, context):
        try:
            return getattr(self.store, name)(request, timeout=10)
        except grpc.RpcError as error:
            context.abort(error.code(), error.details())
    return method


for method_name in ('CheckIn', 'RegisterManifest', 'UpdateJobStatus', 'GetManifest',
                    'CreateJob', 'RegisterVehicle', 'GetVehicle', 'ReportInventory',
                    'RecordVehicleEvent', 'ListPendingJobs'):
    setattr(OtaControlServicer, method_name, forward(method_name))


def serve():
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    rpc.add_OtaControlServicer_to_server(OtaControlServicer(), server)
    server.add_insecure_port('[::]:50051')
    server.start()
    server.wait_for_termination()


if __name__ == '__main__':
    serve()
