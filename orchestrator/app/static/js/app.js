async function loadApps() {
  const resp = await fetch('/apps');
  if (resp.status === 401) {
    window.location.href = '/login';
    return;
  }
  const apps = await resp.json();
  const tbody = document.querySelector('#apps-table tbody');
  if (!tbody) {
    return;
  }
  tbody.innerHTML = '';
  apps.forEach(app => {
    const tr = document.createElement('tr');
    tr.dataset.id = app.id;
    tr.dataset.name = app.name;
    tr.dataset.url = app.url;
    tr.dataset.token = app.token;
    tr.dataset.schedule = app.schedule ?? '';
    tr.dataset.driveFolderId = app.drive_folder_id ?? '';
    tr.dataset.rcloneRemote = app.rclone_remote ?? '';
    tr.dataset.retention = app.retention ?? '';
    tr.innerHTML = `<td>${app.name}</td><td>${app.url}</td><td>${app.token}</td><td>${app.drive_folder_id ?? ''}</td><td>${app.rclone_remote ?? ''}</td><td>${app.retention ?? ''}</td><td><button class="btn btn-sm btn-secondary edit-btn">Edit</button> <button class="btn btn-sm btn-danger delete-btn">Delete</button> <button class="btn btn-sm btn-info run-btn">Run now</button></td>`;
    tbody.appendChild(tr);
  });
}

document.addEventListener('DOMContentLoaded', () => {
  if (document.getElementById('apps-table')) {
    loadApps();
  }

  const addBtn = document.querySelector('[data-bs-target="#appModal"]');
  if (addBtn) {
    addBtn.addEventListener('click', () => {
      const form = document.getElementById('app-form');
      if (form) {
        form.reset();
      }
      const idInput = document.getElementById('app_id');
      if (idInput) {
        idInput.value = '';
      }
      const label = document.getElementById('appModalLabel');
      if (label) {
        label.textContent = 'Register App';
      }
    });
  }

  const form = document.getElementById('app-form');
  if (form) {
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const id = document.getElementById('app_id').value;
      const payload = {
        name: document.getElementById('name').value,
        url: document.getElementById('url').value,
        token: document.getElementById('token').value,
        schedule: document.getElementById('schedule').value || null,
        drive_folder_id: document.getElementById('drive_folder_id').value,
        rclone_remote: document.getElementById('rclone_remote').value,
        retention: document.getElementById('retention').value ? parseInt(document.getElementById('retention').value, 10) : null
      };
      const resp = await fetch(id ? `/apps/${id}` : '/apps', {
        method: id ? 'PUT' : 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      if (resp.status === 401) {
        window.location.href = '/login';
        return;
      }
      if (resp.ok) {
        form.reset();
        document.getElementById('app_id').value = '';
        const modal = bootstrap.Modal.getInstance(document.getElementById('appModal'));
        modal.hide();
        loadApps();
      }
    });
  }

  const table = document.getElementById('apps-table');
  if (table) {
    table.addEventListener('click', async (e) => {
      const tr = e.target.closest('tr');
      if (!tr) return;
      const id = tr.dataset.id;
      if (e.target.classList.contains('edit-btn')) {
        document.getElementById('app_id').value = id;
        document.getElementById('name').value = tr.dataset.name;
        document.getElementById('url').value = tr.dataset.url;
        document.getElementById('token').value = tr.dataset.token;
        document.getElementById('schedule').value = tr.dataset.schedule;
        document.getElementById('drive_folder_id').value = tr.dataset.driveFolderId;
        document.getElementById('rclone_remote').value = tr.dataset.rcloneRemote;
        document.getElementById('retention').value = tr.dataset.retention;
        document.getElementById('appModalLabel').textContent = 'Edit App';
        const modal = new bootstrap.Modal(document.getElementById('appModal'));
        modal.show();
      }
      if (e.target.classList.contains('delete-btn')) {
        if (!confirm('Delete this app?')) return;
        const resp = await fetch(`/apps/${id}`, { method: 'DELETE' });
        if (resp.status === 401) {
          window.location.href = '/login';
          return;
        }
        if (resp.ok) {
          loadApps();
        }
      }
      if (e.target.classList.contains('run-btn')) {
        const resp = await fetch(`/apps/${id}/run`, { method: 'POST' });
        if (resp.status === 401) {
          window.location.href = '/login';
          return;
        }
        if (resp.ok) {
          alert('Backup started');
        }
      }
    });
  }
});
