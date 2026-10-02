from flask import Flask, request, render_template, jsonify
import os, time, uuid
import grpc
from can_bus import CanRPC
from ota_agent import OTAAgent

app = Flask(__name__)
VEHICLE_ID = os.getenv("VEHICLE_ID", "VIN_SIM_0001")
ECU_NAMES = {"engine": "Engine control ECU", "adas": "ADAS ECU"}

# Initialize Shared CAN RPC
# Gateway Listen: 0x001 (Self) - but responses come to 0x101/0x201
can_rpc = CanRPC(0x001)

# Initialize OTA Agent
ota_agent = OTAAgent(VEHICLE_ID, can_rpc)

@app.route("/")
def index():
    return render_template("index.html", vehicle_id=VEHICLE_ID, ecu_names=ECU_NAMES)

@app.route("/api/approve", methods=["POST"])
def api_approve():
    data = request.json or {}
    simulate_failure = data.get("simulate_failure", False)
    
    ok, reason = ota_agent.approve_update(simulate_failure)
    if ok:
        return jsonify(ok=True)
    else:
        return jsonify(ok=False, error=reason)

@app.route("/api/vehicle/state", methods=["GET", "POST"])
def api_vehicle_state():
    if request.method == "POST":
        data = request.json or {}
        try:
            ota_agent.set_vehicle_state(data)
        except ValueError as error:
            return jsonify(ok=False, error=str(error)), 400
        return jsonify(ok=True)
    else:
        # Return defaults if not set
        if not hasattr(ota_agent, 'vehicle_state'):
             ota_agent.set_vehicle_state({})
        return jsonify(ota_agent.vehicle_state)

@app.route('/api/registration', methods=['GET', 'POST'])
def api_registration():
    try:
        record = (ota_agent.cp_client.register_vehicle(request.get_json() or {})
                  if request.method == 'POST' else ota_agent.cp_client.get_vehicle())
        return jsonify(record)
    except grpc.RpcError as error:
        return jsonify(ok=False, error=error.details()), 400 if error.code() == grpc.StatusCode.INVALID_ARGUMENT else 503

@app.route('/api/vehicle/events', methods=['POST'])
def api_vehicle_event():
    data = request.get_json() or {}
    if data.get('kind') not in ('crash', 'diagnostic') or not isinstance(data.get('details'), dict):
        return jsonify(ok=False, error='Provide crash/diagnostic kind and a details object'), 400
    try:
        event_id = data.get('event_id') or str(uuid.uuid4())
        ok = ota_agent.cp_client.record_event(event_id, data['kind'], data['details'])
        return jsonify(ok=ok, event_id=event_id)
    except grpc.RpcError as error:
        return jsonify(ok=False, error=error.details()), 503

@app.route("/api/status", methods=["GET"])
def api_status():
    # Manifest targets describe the proposed update, not installed inventory.
    targets = {
        target["ecu_id"]: target
        for target in (ota_agent.manifest or {}).get("targets", [])
    }
    ecus = {
        ecu_id: {
            "current_version": ota_agent.inventory.get(ecu_id, {}).get("current_version"),
            "previous_version": ota_agent.inventory.get(ecu_id, {}).get("previous_version"),
            "new_version": targets.get(ecu_id, {}).get("target_version"),
            "base_version": targets.get(ecu_id, {}).get("base_version"),
            "component_name": targets.get(ecu_id, {}).get("component_name"),
            "artifact_type": targets.get(ecu_id, {}).get("artifact_type"),
            "artifact_size": targets.get(ecu_id, {}).get("download_size"),
            "release_notes": targets.get(ecu_id, {}).get("release_notes"),
        }
        for ecu_id in ECU_NAMES
    }
    return jsonify({
        "approved": ota_agent.user_approved,
        "simulate_failure": getattr(ota_agent, "simulate_failure", False),
        "state": ota_agent.state,
        "job_id": ota_agent.job_id,
        "campaign_id": ota_agent.campaign_id,
        "ecus": ecus,
        "details": {"reason": getattr(ota_agent, "failure_reason", None)} # Pass failure reason if any
    })

@app.route("/api/progress", methods=["GET"])
def api_progress():
    # Map agent progress to UI expectation
    # UI expects: {"engine": {percent...}, "adas": ...}
    # We'll simplify and show global progress for both ECUs for now, or spoof individual
    p = ota_agent.progress["percent"]
    status = ota_agent.progress["status"]
    
    prog_obj = {
        "engine": {"percent": p, "status": status, "current": p, "total": 100},
        "adas":   {"percent": p, "status": status, "current": p, "total": 100}
    }
    
    # We can add logs from a log buffer if we want, but for now just basic status
    return jsonify({"progress": prog_obj, "logs": [f"State: {ota_agent.state}"]})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
