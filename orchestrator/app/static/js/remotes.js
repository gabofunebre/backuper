const directoryCache = {};
let driveValidation = { status: 'idle', token: '' };
const sftpState = {
  credentials: null,
  currentPath: '/',
  parentPath: '/',
};

function normalizeSftpPath(path) {
  if (!path) return '/';
  let value = path.trim();
  if (!value || value === '.' || value === './') {
    return '/';
  }
  value = value.replace(/\\/g, '/');
  if (!value.startsWith('/')) {
    value = `/${value}`;
  }
  value = value.replace(/\/+/g, '/');
  if (value.length > 1 && value.endsWith('/')) {
    value = value.slice(0, -1);
  }
  return value || '/';
}

function updateSftpStatus(message, variant = 'muted') {
  const target = document.getElementById('sftp-browser-feedback');
  if (!target) return;
  target.classList.remove('text-danger', 'text-success', 'text-muted');
  if (!message) {
    target.textContent = '';
    target.classList.add('text-muted');
    return;
  }
  target.textContent = message;
  if (variant === 'danger') {
    target.classList.add('text-danger');
  } else if (variant === 'success') {
    target.classList.add('text-success');
  } else {
    target.classList.add('text-muted');
  }
}

function updateSftpSelectedPath(path) {
  const input = document.getElementById('sftp_base_path');
  const summary = document.getElementById('sftp-selected-path');
  if (!input || !summary) return;
  summary.classList.remove('text-success', 'text-muted', 'text-danger');
  if (!path) {
    input.value = '';
    summary.textContent = 'Todavía no elegiste la carpeta donde se crearán los respaldos.';
    summary.classList.add('text-muted');
    return;
  }
  const normalized = normalizeSftpPath(path);
  input.value = normalized;
  summary.textContent = `Se va a crear una carpeta con el nombre del remote dentro de ${normalized}.`;
  summary.classList.add('text-success');
}

function resetSftpBrowser(clearSelection = true) {
  sftpState.credentials = null;
  sftpState.currentPath = '/';
  sftpState.parentPath = '/';
  const panel = document.getElementById('sftp-browser-panel');
  if (panel) {
    panel.classList.add('d-none');
  }
  const list = document.getElementById('sftp-directory-list');
  if (list) {
    list.innerHTML = '';
  }
  const emptyAlert = document.getElementById('sftp-empty');
  if (emptyAlert) {
    emptyAlert.classList.add('d-none');
  }
  const currentPathLabel = document.getElementById('sftp-current-path');
  if (currentPathLabel) {
    currentPathLabel.textContent = '/';
  }
  const upButton = document.getElementById('sftp-browser-up');
  if (upButton) {
    upButton.disabled = true;
  }
  updateSftpStatus('Completá las credenciales y listá las carpetas disponibles.', 'muted');
  if (clearSelection) {
    updateSftpSelectedPath('');
  }
}

async function fetchSftpDirectories(path) {
  if (!sftpState.credentials) {
    return null;
  }
  const payload = { ...sftpState.credentials, path };
  const resp = await fetch('/rclone/remotes/sftp/browse', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });
  if (resp.status === 401) {
    window.location.href = '/login';
    return null;
  }
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    const message = data && data.error ? data.error : 'No se pudieron listar las carpetas del servidor SFTP.';
    updateSftpStatus(message, 'danger');
    return null;
  }
  return data;
}

function renderSftpBrowser(data) {
  const panel = document.getElementById('sftp-browser-panel');
  const list = document.getElementById('sftp-directory-list');
  const emptyAlert = document.getElementById('sftp-empty');
  const currentPathLabel = document.getElementById('sftp-current-path');
  const upButton = document.getElementById('sftp-browser-up');
  if (!panel || !list || !currentPathLabel) {
    return;
  }
  panel.classList.remove('d-none');
  list.innerHTML = '';
  const directories = Array.isArray(data.directories) ? data.directories : [];
  sftpState.currentPath = normalizeSftpPath(data.current_path);
  sftpState.parentPath = normalizeSftpPath(data.parent_path);
  currentPathLabel.textContent = sftpState.currentPath;
  if (upButton) {
    upButton.disabled = sftpState.currentPath === '/' || sftpState.parentPath === sftpState.currentPath;
  }
  if (directories.length === 0) {
    if (emptyAlert) {
      emptyAlert.classList.remove('d-none');
    }
    return;
  }
  if (emptyAlert) {
    emptyAlert.classList.add('d-none');
  }
  directories
    .slice()
    .sort((a, b) => a.name.localeCompare(b.name, 'es', { sensitivity: 'base' }))
    .forEach((entry) => {
      if (!entry || !entry.name || !entry.path) {
        return;
      }
      const item = document.createElement('button');
      item.type = 'button';
      item.className = 'list-group-item list-group-item-action';
      item.textContent = entry.name;
      item.dataset.path = entry.path;
      list.appendChild(item);
    });
}

async function openSftpPath(path) {
  if (!sftpState.credentials) {
    updateSftpStatus('Probá la conexión antes de navegar las carpetas.', 'danger');
    return;
  }
  updateSftpStatus('Cargando carpetas…', 'muted');
  const data = await fetchSftpDirectories(path);
  if (data) {
    renderSftpBrowser(data);
    updateSftpStatus('Seleccioná la carpeta donde querés guardar los respaldos.', 'muted');
  }
}

function initSftpBrowser() {
  resetSftpBrowser(true);
  const browseButton = document.getElementById('sftp-browse');
  const list = document.getElementById('sftp-directory-list');
  const upButton = document.getElementById('sftp-browser-up');
  const useButton = document.getElementById('sftp-use-current');
  if (!browseButton || !list || !upButton || !useButton) {
    return;
  }

  browseButton.addEventListener('click', async () => {
    const host = document.getElementById('sftp_host')?.value.trim() || '';
    const portValue = document.getElementById('sftp_port')?.value.trim() || '';
    const username = document.getElementById('sftp_username')?.value.trim() || '';
    const password = document.getElementById('sftp_password')?.value || '';
    if (!host || !username || !password) {
      updateSftpStatus('Completá host, usuario y contraseña antes de listar las carpetas.', 'danger');
      return;
    }
    if (portValue && !/^\d+$/.test(portValue)) {
      updateSftpStatus('El puerto SFTP debe ser un número válido.', 'danger');
      return;
    }
    sftpState.credentials = { host, username, password };
    if (portValue) {
      sftpState.credentials.port = portValue;
    }
    updateSftpSelectedPath('');
    updateSftpStatus('Conectando con el servidor SFTP…', 'muted');
    browseButton.disabled = true;
    try {
      const data = await fetchSftpDirectories('/');
      if (data) {
        renderSftpBrowser(data);
        updateSftpStatus('Seleccioná la carpeta donde querés guardar los respaldos.', 'muted');
      }
    } catch (err) {
      updateSftpStatus('No se pudieron listar las carpetas del servidor SFTP.', 'danger');
    } finally {
      browseButton.disabled = false;
    }
  });

  list.addEventListener('click', async (event) => {
    const target = event.target instanceof Element ? event.target.closest('button[data-path]') : null;
    if (!target) {
      return;
    }
    const { path } = target.dataset;
    if (!path) {
      return;
    }
    await openSftpPath(path);
  });

  upButton.addEventListener('click', async () => {
    if (upButton.disabled) {
      return;
    }
    await openSftpPath(sftpState.parentPath);
  });

  useButton.addEventListener('click', () => {
    if (!sftpState.credentials) {
      updateSftpStatus('Probá la conexión antes de elegir una carpeta.', 'danger');
      return;
    }
    updateSftpSelectedPath(sftpState.currentPath);
    updateSftpStatus('Carpeta seleccionada. Guardá el formulario para crear el remote.', 'success');
  });

  ['sftp_host', 'sftp_port', 'sftp_username', 'sftp_password'].forEach((id) => {
    const field = document.getElementById(id);
    if (!field) {
      return;
    }
    field.addEventListener('input', () => {
      resetSftpBrowser(true);
    });
  });
}

function getDriveMode() {
  const selected = document.querySelector('input[name="drive_mode"]:checked');
  return selected ? selected.value : 'shared';
}

function updateDriveModeUI() {
  const mode = getDriveMode();
  const shared = document.getElementById('drive_shared_settings');
  const custom = document.getElementById('drive_custom_settings');
  if (shared) {
    shared.classList.toggle('d-none', mode !== 'shared');
  }
  if (custom) {
    custom.classList.toggle('d-none', mode !== 'custom');
  }
  if (mode === 'custom') {
    driveValidation = { status: 'idle', token: '' };
    updateDriveFeedback('Recordá probar el token antes de guardar.', 'warning');
  } else {
    driveValidation = { status: 'idle', token: '' };
    updateDriveFeedback('', 'muted');
  }
}

async function loadRemotes() {
  try {
    const resp = await fetch('/rclone/remotes');
    if (resp.status === 401) {
      window.location.href = '/login';
      return;
    }
    const remotes = await resp.json();
    const tbody = document.querySelector('#remotes-table tbody');
    const emptyMessage = document.getElementById('remotes-empty');
    if (tbody) {
      tbody.innerHTML = '';
    }
    const select = document.getElementById('rclone_remote');
    if (select) {
      select.innerHTML = '<option value=""></option>';
    }
    if (emptyMessage) {
      if (!remotes.length) {
        emptyMessage.classList.remove('d-none');
      } else {
        emptyMessage.classList.add('d-none');
      }
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
  if (type !== 'sftp') {
    resetSftpBrowser(true);
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
  } else if (type === 'drive') {
    updateDriveModeUI();
  } else if (type === 'sftp') {
    resetSftpBrowser(false);
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
    if (getDriveMode() !== 'custom') {
      driveValidation = { status: 'idle', token: '' };
      updateDriveFeedback('', 'muted');
      return;
    }
    if (driveValidation.status === 'success' && driveValidation.token !== tokenInput.value.trim()) {
      updateDriveFeedback('El token cambió, probalo nuevamente antes de guardar.', 'warning');
      driveValidation = { status: 'dirty', token: '' };
    } else if (!tokenInput.value.trim()) {
      updateDriveFeedback('', 'muted');
      driveValidation = { status: 'idle', token: '' };
    }
  });
  ['drive_client_id', 'drive_client_secret'].forEach((id) => {
    const field = document.getElementById(id);
    if (!field) {
      return;
    }
    field.addEventListener('input', () => {
      if (getDriveMode() !== 'custom') {
        return;
      }
      if (driveValidation.status === 'success') {
        updateDriveFeedback('Las credenciales cambiaron, probá el token nuevamente.', 'warning');
        driveValidation = { status: 'dirty', token: '' };
      }
    });
  });
  testButton.addEventListener('click', async () => {
    if (getDriveMode() !== 'custom') {
      updateDriveFeedback('Seleccioná "Usar mi propia cuenta" para probar el token.', 'warning');
      return;
    }
    const token = tokenInput.value.trim();
    if (!token) {
      updateDriveFeedback('Pegá el token de Google Drive antes de probarlo.', 'danger');
      return;
    }
    testButton.disabled = true;
    updateDriveFeedback('Probando token…', 'warning');
    try {
      const clientId = document.getElementById('drive_client_id')?.value.trim() || '';
      const clientSecret = document.getElementById('drive_client_secret')?.value.trim() || '';
      const payload = { token };
      if (clientId) {
        payload.client_id = clientId;
      }
      if (clientSecret) {
        payload.client_secret = clientSecret;
      }
      const resp = await fetch('/rclone/remotes/drive/validate', {
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
  initSftpBrowser();
  const typeSelect = document.getElementById('remote_type');
  if (typeSelect) {
    showPanelForType(typeSelect.value);
    typeSelect.addEventListener('change', (event) => {
      const selected = event.target.value;
      showFeedback('', 'info');
      showPanelForType(selected);
    });
  }
  document.querySelectorAll('input[name="drive_mode"]').forEach((input) => {
    input.addEventListener('change', (event) => {
      const mode = event.target.value;
      updateDriveModeUI();
      if (mode !== 'custom') {
        updateDriveFeedback('', 'muted');
      }
    });
  });
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
      const host = document.getElementById('sftp_host')?.value.trim() || '';
      const portValue = document.getElementById('sftp_port')?.value.trim() || '';
      const username = document.getElementById('sftp_username')?.value.trim() || '';
      const password = document.getElementById('sftp_password')?.value || '';
      if (!host) {
        showFeedback('Completá el host del servidor SFTP.', 'danger');
        return;
      }
      if (!username) {
        showFeedback('Indicá el usuario para la conexión SFTP.', 'danger');
        return;
      }
      if (!password) {
        showFeedback('Ingresá la contraseña del usuario SFTP.', 'danger');
        return;
      }
      if (portValue && !/^\d+$/.test(portValue)) {
        showFeedback('El puerto SFTP debe ser un número válido.', 'danger');
        return;
      }
      payload.settings.host = host;
      payload.settings.username = username;
      payload.settings.password = password;
      if (portValue) {
        payload.settings.port = portValue;
      }
      const basePath = document.getElementById('sftp_base_path')?.value.trim() || '';
      if (!basePath) {
        showFeedback('Seleccioná la carpeta del servidor SFTP donde se crearán los respaldos.', 'danger');
        return;
      }
      payload.settings.base_path = basePath;
  } else if (type === 'drive') {
    const mode = getDriveMode();
      payload.settings.mode = mode;
      if (mode === 'shared') {
        const emailInput = document.getElementById('drive_email');
        const email = emailInput ? emailInput.value.trim() : '';
        const emailRegex = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
        if (!email) {
          showFeedback('Ingresá el correo de Google con el que querés compartir la carpeta.', 'danger');
          return;
        }
        if (!emailRegex.test(email)) {
          showFeedback('El correo ingresado no tiene un formato válido.', 'danger');
          return;
        }
        payload.settings.email = email;
      } else {
        const token = document.getElementById('drive_token')?.value.trim() || '';
        if (!token) {
          showFeedback('Pegá el token de Google Drive antes de guardar.', 'danger');
          return;
        }
        if (driveValidation.status === 'success' && driveValidation.token !== token) {
          driveValidation = { status: 'dirty', token: '' };
        }
        payload.settings.token = token;
        const clientId = document.getElementById('drive_client_id')?.value.trim() || '';
        const clientSecret = document.getElementById('drive_client_secret')?.value.trim() || '';
        const accountEmail = document.getElementById('drive_account_email')?.value.trim() || '';
        if (clientId) {
          payload.settings.client_id = clientId;
        }
        if (clientSecret) {
          payload.settings.client_secret = clientSecret;
        }
        if (accountEmail) {
          payload.settings.account_email = accountEmail;
        }
        if (driveValidation.status !== 'success') {
          showFeedback('Probá el token de Google Drive antes de guardarlo.', 'warning');
          return;
        }
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
        resetSftpBrowser(true);
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
