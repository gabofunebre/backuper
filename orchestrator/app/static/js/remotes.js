const directoryCache = {};
let driveValidation = { status: 'idle', token: '' };

async function loadRemotes() {
  try {
    const resp = await fetch('/rclone/remotes');
    if (resp.status === 401) {
      window.location.href = '/login';
      return;
    }
    const remotes = await resp.json();
    const tbody = document.querySelector('#remotes-table tbody');
    if (tbody) {
      tbody.innerHTML = '';
    }
    const select = document.getElementById('rclone_remote');
    if (select) {
      select.innerHTML = '<option value=""></option>';
    }
    remotes.forEach((name) => {
      if (tbody) {
        const tr = document.createElement('tr');
        tr.innerHTML = `<td>${name}</td>`;
        tbody.appendChild(tr);
      }
      if (select) {
        const opt = document.createElement('option');
        opt.value = name;
        opt.textContent = name;
        select.appendChild(opt);
      }
    });
  } catch (error) {
    console.error('No se pudieron cargar los remotes configurados', error);
  }
}

function showFeedback(message, type = 'info') {
  const feedback = document.getElementById('remote-feedback');
  if (!feedback) return;
  const baseClass = 'alert mt-4';
  if (!message) {
    feedback.className = `${baseClass} d-none`;
    feedback.textContent = '';
    return;
  }
  feedback.className = `${baseClass} alert-${type}`;
  feedback.textContent = message;
}

function updateDriveFeedback(message, variant = 'muted') {
  const target = document.getElementById('drive-token-feedback');
  if (!target) return;
  target.classList.remove('text-success', 'text-danger', 'text-warning');
  if (!message) {
    target.textContent = '';
    target.classList.add('text-muted');
    return;
  }
  target.textContent = message;
  target.classList.remove('text-muted');
  if (variant === 'success') {
    target.classList.add('text-success');
  } else if (variant === 'warning') {
    target.classList.add('text-warning');
  } else if (variant === 'danger') {
    target.classList.add('text-danger');
  } else {
    target.classList.add('text-muted');
  }
}

function resetPanels() {
  document.querySelectorAll('[data-remote-panel]').forEach((panel) => {
    panel.classList.add('d-none');
  });
}

async function fetchDirectoryOptions(type) {
  if (directoryCache[type]) {
    return directoryCache[type];
  }
  const resp = await fetch(`/rclone/remotes/options/${type}`);
  if (resp.status === 401) {
    window.location.href = '/login';
    return {};
  }
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    const message = data && data.error ? data.error : 'No se pudo cargar la configuración';
    throw new Error(message);
  }
  directoryCache[type] = data;
  return data;
}

function populateDirectorySelect(select, directories, emptyMessageId) {
  if (!select) return;
  const emptyMessage = emptyMessageId ? document.getElementById(emptyMessageId) : null;
  select.innerHTML = '';
  if (!directories || directories.length === 0) {
    select.disabled = true;
    if (emptyMessage) {
      emptyMessage.classList.remove('d-none');
    }
    return;
  }
  if (emptyMessage) {
    emptyMessage.classList.add('d-none');
  }
  const placeholder = document.createElement('option');
  placeholder.value = '';
  placeholder.textContent = 'Seleccione…';
  select.appendChild(placeholder);
  directories.forEach((entry) => {
    const option = document.createElement('option');
    option.value = entry.path;
    if (entry.label && entry.label !== entry.path) {
      option.textContent = `${entry.label} — ${entry.path}`;
    } else {
      option.textContent = entry.path;
    }
    select.appendChild(option);
  });
  select.disabled = false;
}

async function showPanelForType(type) {
  resetPanels();
  updateDriveFeedback('', 'muted');
  if (!type) {
    return;
  }
  const panel = document.querySelector(`[data-remote-panel="${type}"]`);
  if (!panel) {
    return;
  }
  panel.classList.remove('d-none');
  if (type === 'local') {
    const select = document.getElementById('local_path');
    try {
      const data = await fetchDirectoryOptions('local');
      populateDirectorySelect(select, data.directories || [], 'local-empty');
    } catch (err) {
      if (select) {
        select.innerHTML = '<option value="">No se pudieron cargar las carpetas</option>';
        select.disabled = true;
      }
      showFeedback(err.message, 'danger');
    }
  } else if (type === 'sftp') {
    const select = document.getElementById('sftp_path');
    const details = document.getElementById('sftp-details');
    try {
      const data = await fetchDirectoryOptions('sftp');
      populateDirectorySelect(select, data.directories || [], 'sftp-empty');
      if (details) {
        const host = data.host;
        const port = data.port ? `:${data.port}` : '';
        if (host) {
          details.textContent = `Conexión configurada: ${host}${port}`;
          details.classList.remove('d-none');
        } else {
          details.textContent = '';
          details.classList.add('d-none');
        }
      }
    } catch (err) {
      if (select) {
        select.innerHTML = '<option value="">No se pudieron cargar las carpetas</option>';
        select.disabled = true;
      }
      if (details) {
        details.textContent = '';
        details.classList.add('d-none');
      }
      showFeedback(err.message, 'danger');
    }
  } else if (type === 'drive') {
    driveValidation = { status: 'idle', token: '' };
    updateDriveFeedback('Recordá probar el token antes de guardar.', 'warning');
  } else if (type === 'onedrive') {
    showFeedback('La integración con OneDrive está en construcción.', 'info');
  }
}

function initDriveValidation() {
  const tokenInput = document.getElementById('drive_token');
  const testButton = document.getElementById('drive-test-token');
  if (!tokenInput || !testButton) {
    return;
  }
  tokenInput.addEventListener('input', () => {
    if (driveValidation.status === 'success' && driveValidation.token !== tokenInput.value.trim()) {
      updateDriveFeedback('El token cambió, probalo nuevamente antes de guardar.', 'warning');
      driveValidation = { status: 'dirty', token: '' };
    } else if (!tokenInput.value.trim()) {
      updateDriveFeedback('', 'muted');
      driveValidation = { status: 'idle', token: '' };
    }
  });
  testButton.addEventListener('click', async () => {
    const token = tokenInput.value.trim();
    if (!token) {
      updateDriveFeedback('Pegá el token de Google Drive antes de probarlo.', 'danger');
      return;
    }
    testButton.disabled = true;
    updateDriveFeedback('Probando token…', 'warning');
    try {
      const resp = await fetch('/rclone/remotes/drive/validate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token })
      });
      if (resp.status === 401) {
        window.location.href = '/login';
        return;
      }
      const data = await resp.json().catch(() => ({}));
      if (resp.ok) {
        updateDriveFeedback('Token válido. Ya podés guardar el remote.', 'success');
        driveValidation = { status: 'success', token };
      } else {
        const message = data && data.error ? data.error : 'No se pudo validar el token';
        updateDriveFeedback(message, 'danger');
        driveValidation = { status: 'error', token: '' };
      }
    } catch (err) {
      updateDriveFeedback('Error al comunicarse con el servidor para validar el token.', 'danger');
      driveValidation = { status: 'error', token: '' };
    } finally {
      testButton.disabled = false;
    }
  });
}

function initRemoteForm() {
  const form = document.getElementById('remote-form');
  if (!form) {
    return;
  }
  const typeSelect = document.getElementById('remote_type');
  if (typeSelect) {
    showPanelForType(typeSelect.value);
    typeSelect.addEventListener('change', (event) => {
      const selected = event.target.value;
      showFeedback('', 'info');
      showPanelForType(selected);
    });
  }
  initDriveValidation();
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    showFeedback('', 'info');
    const nameInput = document.getElementById('remote_name');
    const type = typeSelect ? typeSelect.value : '';
    const name = nameInput ? nameInput.value.trim() : '';
    if (!name) {
      showFeedback('Completá un nombre para el remote.', 'danger');
      return;
    }
    if (!type) {
      showFeedback('Elegí el tipo de remote que querés configurar.', 'danger');
      return;
    }

    const payload = { name, type, settings: {} };
    if (type === 'local') {
      const select = document.getElementById('local_path');
      const value = select ? select.value : '';
      if (!value) {
        showFeedback('Seleccioná la carpeta local donde guardar los respaldos.', 'danger');
        return;
      }
      payload.settings.path = value;
    } else if (type === 'sftp') {
      const select = document.getElementById('sftp_path');
      const value = select ? select.value : '';
      if (!value) {
        showFeedback('Elegí la carpeta del servidor SFTP para este remote.', 'danger');
        return;
      }
      payload.settings.path = value;
    } else if (type === 'drive') {
      const token = document.getElementById('drive_token')?.value.trim() || '';
      if (!token) {
        showFeedback('Pegá el token de Google Drive antes de guardar.', 'danger');
        return;
      }
      if (driveValidation.status === 'success' && driveValidation.token !== token) {
        driveValidation = { status: 'dirty', token: '' };
      }
      payload.settings.token = token;
      if (driveValidation.status !== 'success') {
        showFeedback('Probá el token de Google Drive antes de guardarlo.', 'warning');
        return;
      }
    } else if (type === 'onedrive') {
      showFeedback('OneDrive todavía no está disponible.', 'warning');
      return;
    }

    const submitBtn = form.querySelector('button[type="submit"]');
    if (submitBtn) {
      submitBtn.disabled = true;
    }
    try {
      const resp = await fetch('/rclone/remotes', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      if (resp.status === 401) {
        window.location.href = '/login';
        return;
      }
      const data = await resp.json().catch(() => ({}));
      if (resp.ok) {
        form.reset();
        resetPanels();
        driveValidation = { status: 'idle', token: '' };
        updateDriveFeedback('', 'muted');
        delete directoryCache.local;
        delete directoryCache.sftp;
        showFeedback('Remote guardado correctamente.', 'success');
        loadRemotes();
      } else {
        const message = data && data.error ? data.error : 'No se pudo crear el remote';
        showFeedback(message, 'danger');
      }
    } catch (err) {
      showFeedback('Ocurrió un error al comunicarse con el servidor.', 'danger');
    } finally {
      if (submitBtn) {
        submitBtn.disabled = false;
      }
    }
  });
}

document.addEventListener('DOMContentLoaded', () => {
  loadRemotes();
  initRemoteForm();
});
