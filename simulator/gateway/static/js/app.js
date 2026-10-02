const byId = (id) => document.getElementById(id);
let latestStatus = null;
let connected = false;
let submitting = false;
let approvalPending = false;
let pendingCampaign = null;

async function requestJSON(path, options = {}, timeoutMs = 8000) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), timeoutMs);
    try {
        const response = await fetch(path, { ...options, signal: controller.signal });
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || `Request failed (${response.status}).`);
        return result;
    } finally {
        clearTimeout(timeout);
    }
}

function message(id, text, error = false) {
    byId(id).textContent = text;
    byId(id).dataset.error = String(error);
    byId(id).hidden = !text;
}

function showPage() {
    const pages = ['ota', 'registration', 'simulation'];
    const requested = location.hash.slice(1);
    const page = pages.includes(requested) ? requested : 'ota';
    pages.forEach((name) => { byId(`${name}-page`).hidden = name !== page; });
    message('action-message', '');
    document.querySelectorAll('nav a').forEach((link) => {
        if (link.dataset.page === page) link.setAttribute('aria-current', 'page');
        else link.removeAttribute('aria-current');
    });
}

function updateUpgradeButton() {
    byId('upgrade').disabled = !connected || submitting || approvalPending
        || latestStatus?.state !== 'WAITING_FOR_APPROVAL'
        || Boolean(latestStatus?.approved);
}

function renderStatus(status) {
    latestStatus = status;
    if (pendingCampaign !== status.campaign_id || status.state !== 'WAITING_FOR_APPROVAL') {
        approvalPending = false;
    }
    byId('ota-state').textContent = status.state;
    byId('campaign-id').textContent = status.campaign_id || '—';
    let hasDetails = false;
    for (const ecu of ['engine', 'adas']) {
        const versions = status.ecus?.[ecu] || {};
        byId(`${ecu}-current`).textContent = versions.current_version || '—';
        byId(`${ecu}-previous`).textContent = versions.previous_version || '—';
        byId(`${ecu}-new`).textContent = versions.new_version || '—';
        byId(`${ecu}-details`).hidden = !versions.new_version;
        if (versions.new_version) {
            hasDetails = true;
            byId(`${ecu}-version-change`).textContent = `${versions.base_version || '—'} → ${versions.new_version}`;
            byId(`${ecu}-component`).textContent = versions.component_name || '—';
            const type = { delta: 'Delta patch', full: 'Full firmware' }[versions.artifact_type]
                || versions.artifact_type || '—';
            const size = Number.isFinite(versions.artifact_size)
                ? ` · ${versions.artifact_size.toLocaleString()} bytes` : '';
            byId(`${ecu}-download`).textContent = type + size;
            byId(`${ecu}-release-notes`).textContent = typeof versions.release_notes === 'string' && versions.release_notes.trim()
                ? versions.release_notes : 'No change description provided.';
        }
    }
    byId('details-empty').hidden = hasDetails;
    const reason = status.details?.reason;
    byId('failure-reason').hidden = !reason;
    byId('failure-reason').textContent = reason || '';
    updateUpgradeButton();
}

async function pollStatus() {
    try {
        const [status, progress] = await Promise.all([
            requestJSON('/api/status'), requestJSON('/api/progress'),
        ]);
        connected = true;
        message('connection-status', 'Connected to gateway.');
        renderStatus(status);
        // The API duplicates campaign progress for both ECUs; show it once.
        const value = Number(progress.progress?.engine?.percent);
        const percent = Number.isFinite(value) ? Math.max(0, Math.min(100, value)) : 0;
        byId('campaign-progress').value = percent;
        byId('progress-value').textContent = `${percent}%`;
        byId('progress-status').textContent = (progress.logs || []).join(' · ');
    } catch (error) {
        connected = false;
        message('connection-status', 'Gateway unavailable. Displayed values may be out of date; retrying…', true);
        updateUpgradeButton();
    } finally {
        setTimeout(pollStatus, 2000);
    }
}

async function approve() {
    if (byId('upgrade').disabled) return;
    submitting = true;
    updateUpgradeButton();
    message('action-message', '');
    try {
        const result = await requestJSON('/api/approve', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ simulate_failure: byId('simulate-failure').checked }),
        });
        if (!result.ok) throw new Error(result.error || 'Approval was rejected.');
        approvalPending = true;
        pendingCampaign = latestStatus.campaign_id;
        message('action-message', '');
    } catch (error) {
        message('action-message', error.message, true);
    } finally {
        submitting = false;
        updateUpgradeButton();
    }
}

async function loadVehicleState() {
    try {
        const state = await requestJSON('/api/vehicle/state');
        byId('battery').value = state.battery_soc;
        byId('gear').value = state.gear;
        byId('ignition').value = state.ignition;
        byId('parking-brake').checked = state.parking_brake;
        byId('vehicle-fields').disabled = false;
        message('vehicle-message', '');
    } catch (error) {
        message('vehicle-message', 'Could not load vehicle state. Retrying…', true);
        setTimeout(loadVehicleState, 5000);
    }
}

async function saveVehicleState(event) {
    event.preventDefault();
    const state = {
        battery_soc: Number(byId('battery').value),
        gear: byId('gear').value,
        ignition: byId('ignition').value,
        parking_brake: byId('parking-brake').checked,
    };
    byId('vehicle-fields').disabled = true;
    try {
        const result = await requestJSON('/api/vehicle/state', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(state),
        });
        if (!result.ok) throw new Error(result.error || 'Vehicle state was rejected.');
        message('vehicle-message', 'Vehicle state saved.');
    } catch (error) {
        message('vehicle-message', error.message, true);
    } finally {
        byId('vehicle-fields').disabled = false;
    }
}

window.addEventListener('hashchange', showPage);
byId('upgrade').addEventListener('click', approve);
byId('rollback').addEventListener('click', () => {
    message('action-message', 'Rollback is not available yet.', true);
});
byId('force-stop').addEventListener('click', () => {
    message('action-message', 'Force stop is not available yet.', true);
});
byId('vehicle-form').addEventListener('submit', saveVehicleState);
showPage();
loadVehicleState();
pollStatus();


async function loadRegistration() {
    try {
        const record = await requestJSON('/api/registration');
        const profile = record.profile || {};
        byId('registration-make').value = profile.make || '';
        byId('registration-model').value = profile.model || '';
        byId('registration-year').value = profile.model_year || '';
        byId('registration-type').value = profile.vehicle_type || '';
        byId('registration-features').value = (profile.features || []).join(', ');
        byId('registration-records').textContent = JSON.stringify({ecus: record.ecus, jobs: record.jobs, events: record.events}, null, 2);
        message('registration-message', '');
    } catch (error) {
        message('registration-message', 'Could not load registration: ' + error.message, true);
    }
}
byId('registration-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    try {
        await requestJSON('/api/registration', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({
            make: byId('registration-make').value, model: byId('registration-model').value,
            model_year: byId('registration-year').value, vehicle_type: byId('registration-type').value,
            features: byId('registration-features').value.split(',').map(s => s.trim()).filter(Boolean),
        })});
        await loadRegistration();
        message('registration-message', 'Registration saved.');
    } catch (error) { message('registration-message', error.message, true); }
});
byId('event-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    try {
        const result = await requestJSON('/api/vehicle/events', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({
            kind: byId('event-kind').value, details: {description: byId('event-description').value},
        })});
        if (!result.ok) throw new Error(result.error || 'Event was rejected');
        message('event-message', 'Event recorded.');
        byId('event-description').value = '';
        await loadRegistration();
    } catch (error) { message('event-message', error.message, true); }
});
byId('refresh-registration').addEventListener('click', loadRegistration);
loadRegistration();
