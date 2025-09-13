async function loadApps() {
  const resp = await fetch('/apps');
  const apps = await resp.json();
  const tbody = document.querySelector('#apps-table tbody');
  tbody.innerHTML = '';
  apps.forEach(app => {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${app.name}</td><td>${app.url}</td><td>${app.token}</td>`;
    tbody.appendChild(tr);
  });
}

document.addEventListener('DOMContentLoaded', () => {
  loadApps();

  document.getElementById('app-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const payload = {
      name: document.getElementById('name').value,
      url: document.getElementById('url').value,
      token: document.getElementById('token').value,
      schedule: document.getElementById('schedule').value,
      drive_folder_id: document.getElementById('drive_folder_id').value,
      retention: {
        daily: parseInt(document.getElementById('retention-daily').value) || null,
        weekly: parseInt(document.getElementById('retention-weekly').value) || null
      }
    };
    const resp = await fetch('/apps', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    if (resp.ok) {
      e.target.reset();
      const modal = bootstrap.Modal.getInstance(document.getElementById('appModal'));
      modal.hide();
      loadApps();
    }
  });
});
