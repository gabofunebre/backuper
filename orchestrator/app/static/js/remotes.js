const directoryCache = {};
let driveValidation = { status: 'idle', token: '' };
let editingRemote = null;
let deleteModalInstance = null;
let pendingRemoteDeletion = null;
let adminEmail = '';

function getAdminEmail() {
  if (adminEmail) {
    return adminEmail;
  }
  const configNode = document.getElementById('remote-config-data');
  if (configNode && typeof configNode.dataset.adminEmail === 'string') {
    adminEmail = configNode.dataset.adminEmail || '';
  }
  return adminEmail;
}

function normalizeLocalPath(path) {
  if (path === null || path === undefined) {
    return '';
  }
  let value = String(path).trim();
  if (!value) {
    return '';
  }
  value = value.replace(/\\+/g, '/');
  const driveMatch = value.match(/^([A-Za-z]:)(?:\/)?$/);
  if (driveMatch) {
    return `${driveMatch[1]}/`;
  }
  if (value.length > 1 && value.endsWith('/')) {
    value = value.replace(/\/+$/, '');
    if (!value) {
      value = '/';
    }
  }
  return value;
}

function joinLocalPath(base, name) {
  const normalizedBase = normalizeLocalPath(base);
  const segment = String(name ?? '').trim();
  if (!normalizedBase) {
    return '';
  }
  if (!segment) {
    return normalizedBase;
  }
  if (/^[A-Za-z]:\/$/.test(normalizedBase)) {
    return `${normalizedBase}${segment}`;
  }
  if (/^[A-Za-z]:$/.test(normalizedBase)) {
    return `${normalizedBase}/${segment}`;
  }
  if (normalizedBase === '/') {
    return `/${segment}`;
  }
  return `${normalizedBase}/${segment}`;
}

function getLocalParentPath(path) {
  const normalized = normalizeLocalPath(path);
  if (!normalized) {
    return '';
  }
  if (normalized === '/') {
    return '/';
  }
  if (/^[A-Za-z]:\/$/.test(normalized)) {
    return normalized;
  }
  if (/^[A-Za-z]:$/.test(normalized)) {
    return normalized;
  }
  const index = normalized.lastIndexOf('/');
  if (index <= 0) {
    return normalized.startsWith('/') ? '/' : normalized;
  }
  const parent = normalized.slice(0, index);
  if (!parent) {
    return normalized.startsWith('/') ? '/' : parent;
  }
  if (/^[A-Za-z]:$/.test(parent)) {
    return parent;
  }
  return parent;
}

function updateLocalSummary() {
  const summary = document.getElementById('local-path-summary');
  if (!summary) return;
  const select = document.getElementById('local_path');
  const basePath = select ? select.value.trim() : '';
  const nameInput = document.getElementById('remote_name');
  const remoteName = nameInput ? nameInput.value.trim() : '';
  const editingLocal = Boolean(
    editingRemote && editingRemote.type && editingRemote.type.toLowerCase() === 'local',
  );
  const currentRoute = editingLocal
    ? normalizeLocalPath(editingRemote.route || editingRemote.share_url || '')
    : '';
  const normalizedBasePath = normalizeLocalPath(basePath);
  summary.classList.remove('text-muted', 'text-success', 'text-warning');
  if (!basePath) {
    summary.textContent = 'Elegí una carpeta disponible para preparar el remote.';
    summary.classList.add('text-muted');
    return;
  }
  if (!remoteName) {
    summary.textContent = 'Completá el nombre del remote para confirmar la carpeta a utilizar.';
    summary.classList.add('text-muted');
    return;
  }
  const targetPath = joinLocalPath(basePath, remoteName);
  if (!targetPath) {
    summary.textContent = 'No se pudo determinar la carpeta destino. Revisá los datos ingresados.';
    summary.classList.add('text-warning');
    return;
  }
  const normalizedTarget = normalizeLocalPath(targetPath);
  if (editingLocal && currentRoute) {
    const readableCurrent = editingRemote.route || editingRemote.share_url || currentRoute;
    if (currentRoute === normalizedTarget) {
      summary.textContent = `La carpeta actual se mantendrá en ${targetPath}.`;
    } else if (normalizedBasePath && currentRoute === normalizedBasePath) {
      summary.textContent = `Vamos a crear la carpeta ${targetPath} y mover allí los archivos existentes de ${readableCurrent}.`;
    } else {
      summary.textContent = `La carpeta se moverá de ${readableCurrent} a ${targetPath}.`;
    }
  } else {
    summary.textContent = `Se creará la carpeta ${targetPath} para guardar los respaldos.`;
  }
  summary.classList.add('text-success');
}

function initDeleteModal() {
  const modalElement = document.getElementById('remote-delete-modal');
  if (!modalElement || typeof bootstrap === 'undefined' || !bootstrap.Modal) {
    return;
  }
  deleteModalInstance = new bootstrap.Modal(modalElement);
  const confirmButton = document.getElementById('remote-delete-confirm');
  if (confirmButton) {
    confirmButton.addEventListener('click', async () => {
      if (!pendingRemoteDeletion) {
        deleteModalInstance.hide();
        return;
      }
      const target = pendingRemoteDeletion;
      pendingRemoteDeletion = null;
      confirmButton.disabled = true;
      try {
        deleteModalInstance.hide();
        await handleRemoteDelete(target);
      } finally {
        confirmButton.disabled = false;
      }
    });
  }
  modalElement.addEventListener('hidden.bs.modal', () => {
    pendingRemoteDeletion = null;
  });
}

const sftpState = {
  credentials: null,
  currentPath: '/',
  parentPath: '/',
};

function toggleRemoteOverlay(show, message = 'Procesando…') {
  const overlay = document.getElementById('remote-loading-overlay');
  const text = document.getElementById('remote-loading-text');
  if (!overlay) {
    return;
  }
  if (show) {
    if (text) {
      text.textContent = message;
    }
    overlay.classList.remove('d-none');
  } else {
    overlay.classList.add('d-none');
  }
}

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
  const select = document.getElementById('sftp-directory-select');
  if (select) {
    select.innerHTML = '';
    select.disabled = true;
    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = 'Probá la conexión para listar carpetas';
    placeholder.disabled = true;
    placeholder.selected = true;
    select.appendChild(placeholder);
  }
  const openButton = document.getElementById('sftp-open-selected');
  if (openButton) {
    openButton.disabled = true;
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
  updateSftpStatus('Completá las credenciales y probá la conexión para ver las carpetas disponibles.', 'muted');
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
  const select = document.getElementById('sftp-directory-select');
  const emptyAlert = document.getElementById('sftp-empty');
  const currentPathLabel = document.getElementById('sftp-current-path');
  const upButton = document.getElementById('sftp-browser-up');
  const openButton = document.getElementById('sftp-open-selected');
  if (!panel || !select || !currentPathLabel) {
    return;
  }
  panel.classList.remove('d-none');
  select.innerHTML = '';
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
    const option = document.createElement('option');
    option.value = '';
    option.textContent = 'No hay subcarpetas disponibles';
    option.disabled = true;
    option.selected = true;
    select.appendChild(option);
    select.disabled = true;
    if (openButton) {
      openButton.disabled = true;
    }
    return;
  }
  if (emptyAlert) {
    emptyAlert.classList.add('d-none');
  }
  const placeholder = document.createElement('option');
  placeholder.value = '';
  placeholder.textContent = 'Seleccioná una subcarpeta…';
  placeholder.disabled = true;
  placeholder.selected = true;
  select.appendChild(placeholder);
  directories
    .slice()
    .sort((a, b) => a.name.localeCompare(b.name, 'es', { sensitivity: 'base' }))
    .forEach((entry) => {
      if (!entry || !entry.name || !entry.path) {
        return;
      }
      const option = document.createElement('option');
      option.value = entry.path;
      option.textContent = entry.name;
      select.appendChild(option);
    });
  select.disabled = false;
  if (openButton) {
    openButton.disabled = true;
  }
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
    updateSftpStatus('Seleccioná una carpeta del menú desplegable o navegá con "Ver subcarpetas".', 'muted');
  }
}

function initSftpBrowser() {
  resetSftpBrowser(true);
  const browseButton = document.getElementById('sftp-browse');
  const directorySelect = document.getElementById('sftp-directory-select');
  const upButton = document.getElementById('sftp-browser-up');
  const useButton = document.getElementById('sftp-use-current');
  const openButton = document.getElementById('sftp-open-selected');
  if (!browseButton || !directorySelect || !upButton || !useButton || !openButton) {
    return;
  }

  browseButton.addEventListener('click', async () => {
    const host = document.getElementById('sftp_host')?.value.trim() || '';
    const portValue = document.getElementById('sftp_port')?.value.trim() || '';
    const username = document.getElementById('sftp_username')?.value.trim() || '';
    const password = document.getElementById('sftp_password')?.value || '';
    if (!host || !username || !password) {
      updateSftpStatus('Completá host, usuario y contraseña antes de probar la conexión.', 'danger');
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
        updateSftpStatus('Seleccioná una carpeta del menú desplegable o navegá con "Ver subcarpetas".', 'muted');
      }
    } catch (err) {
      updateSftpStatus('No se pudieron listar las carpetas del servidor SFTP.', 'danger');
    } finally {
      browseButton.disabled = false;
    }
  });

  const handleOpenSelected = async () => {
    if (!directorySelect.value) {
      updateSftpStatus('Elegí una subcarpeta del menú desplegable para continuar.', 'danger');
      return;
    }
    await openSftpPath(directorySelect.value);
  };

  directorySelect.addEventListener('change', () => {
    if (openButton) {
      openButton.disabled = !directorySelect.value;
    }
  });

  directorySelect.addEventListener('dblclick', async () => {
    if (!directorySelect.value) {
      return;
    }
    await openSftpPath(directorySelect.value);
  });

  directorySelect.addEventListener('keydown', async (event) => {
    if (event.key === 'Enter' && directorySelect.value) {
      event.preventDefault();
      await openSftpPath(directorySelect.value);
    }
  });

  openButton.addEventListener('click', handleOpenSelected);

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
    const payload = await resp.json();
    const remotes = Array.isArray(payload) ? payload : [];
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
    remotes.forEach((entry) => {
      const rawRemote = typeof entry === 'string' ? { name: entry } : entry || {};
      const name = (rawRemote.name || '').trim();
      const rawRoute = (rawRemote.route && typeof rawRemote.route === 'string')
        ? rawRemote.route.trim()
        : '';
      const rawShare = (rawRemote.share_url && typeof rawRemote.share_url === 'string')
        ? rawRemote.share_url.trim()
        : '';
      const displayRoute = rawShare || rawRoute;
      const normalizedRemote = {
        ...rawRemote,
        name,
        route: rawRoute,
        share_url: rawShare,
        display_route: displayRoute,
      };
      const remoteType = (normalizedRemote.type || '').toLowerCase();
      if (tbody) {
        const tr = document.createElement('tr');

        const nameCell = document.createElement('td');
        nameCell.textContent = name;
        tr.appendChild(nameCell);

        const linkCell = document.createElement('td');
        if (displayRoute) {
          const looksLikeUrl = /^https?:\/\//i.test(displayRoute);
          if (remoteType === 'drive' && looksLikeUrl) {
            const anchor = document.createElement('a');
            anchor.href = displayRoute;
            anchor.target = '_blank';
            anchor.rel = 'noopener';
            anchor.textContent = displayRoute;
            anchor.classList.add('text-break');
            linkCell.appendChild(anchor);
          } else {
            const span = document.createElement('span');
            span.textContent = displayRoute;
            span.classList.add('text-break');
            linkCell.appendChild(span);
          }
        } else {
          linkCell.textContent = '—';
          linkCell.classList.add('text-muted');
        }
        tr.appendChild(linkCell);

        const actionsCell = document.createElement('td');
        actionsCell.className = 'text-end text-nowrap';

        const editBtn = document.createElement('button');
        editBtn.type = 'button';
        editBtn.className = 'btn btn-outline-secondary btn-sm me-2';
        editBtn.innerHTML = '<span aria-hidden="true">&#9998;</span><span class="visually-hidden">Editar</span>';
        editBtn.addEventListener('click', () => startRemoteEdit(normalizedRemote));
        actionsCell.appendChild(editBtn);

        const deleteBtn = document.createElement('button');
        deleteBtn.type = 'button';
        deleteBtn.className = 'btn btn-outline-danger btn-sm';
        deleteBtn.innerHTML = '<span aria-hidden="true">&times;</span><span class="visually-hidden">Eliminar</span>';
        deleteBtn.addEventListener('click', () => confirmRemoteDeletion(normalizedRemote));
        actionsCell.appendChild(deleteBtn);

        tr.appendChild(actionsCell);
        tbody.appendChild(tr);
      }
      if (select && name) {
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

function showFeedback(message, type = 'info', options = {}) {
  const feedback = document.getElementById('remote-feedback');
  if (!feedback) return;
  const baseClass = 'alert mt-4';
  if (!message) {
    feedback.className = `${baseClass} d-none`;
    feedback.textContent = '';
    return;
  }
  feedback.className = `${baseClass} alert-${type}`;
  feedback.textContent = '';
  feedback.appendChild(document.createTextNode(message));
  if (options.link) {
    const link = document.createElement('a');
    link.href = options.link;
    link.target = '_blank';
    link.rel = 'noopener';
    link.classList.add('d-block', 'mt-2', 'text-break');
    link.textContent = options.link;
    feedback.appendChild(link);
  }
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

function exitRemoteEditMode({ resetForm = false } = {}) {
  editingRemote = null;
  const nameInput = document.getElementById('remote_name');
  const submitButton = document.getElementById('remote-submit');
  const cancelButton = document.getElementById('remote-cancel-edit');
  if (nameInput) {
    nameInput.disabled = false;
  }
  if (submitButton) {
    submitButton.textContent = 'Guardar remote';
  }
  if (cancelButton) {
    cancelButton.classList.add('d-none');
  }
  if (resetForm) {
    const form = document.getElementById('remote-form');
    if (form) {
      form.reset();
    }
    resetPanels();
    driveValidation = { status: 'idle', token: '' };
    updateDriveFeedback('', 'muted');
    resetSftpBrowser(true);
    showFeedback('', 'info');
  }
  updateLocalSummary();
}

function startRemoteEdit(remote) {
  if (!remote || !remote.name) {
    return;
  }
  const form = document.getElementById('remote-form');
  if (!form) {
    return;
  }
  editingRemote = { ...remote };
  form.reset();
  resetPanels();
  driveValidation = { status: 'idle', token: '' };
  updateDriveFeedback('', 'muted');
  resetSftpBrowser(true);
  const nameInput = document.getElementById('remote_name');
  if (nameInput) {
    nameInput.value = remote.name;
    nameInput.focus();
  }
  const typeSelect = document.getElementById('remote_type');
  if (typeSelect) {
    typeSelect.value = remote.type || '';
    showPanelForType(typeSelect.value);
    typeSelect.focus();
  } else {
    showPanelForType('');
  }
  const submitButton = document.getElementById('remote-submit');
  if (submitButton) {
    submitButton.textContent = 'Actualizar remote';
  }
  const cancelButton = document.getElementById('remote-cancel-edit');
  if (cancelButton) {
    cancelButton.classList.remove('d-none');
  }
  showFeedback(
    `Editando el remote ${remote.name}. Podés actualizar los datos e incluso cambiar el nombre antes de guardar.`,
    'info',
  );
  const normalizedType = (remote.type || '').toLowerCase();
  const storedRoute = remote.route || remote.share_url || '';
  if (normalizedType === 'sftp' && storedRoute) {
    updateSftpSelectedPath(storedRoute);
  }
  if (normalizedType === 'local') {
    const select = document.getElementById('local_path');
    if (select) {
      const parentPath = getLocalParentPath(storedRoute);
      if (parentPath) {
        select.value = parentPath;
      }
    }
  }
  updateLocalSummary();
}

function confirmRemoteDeletion(remote) {
  if (!remote || !remote.name) {
    return;
  }
  const remoteType = (remote.type || '').toLowerCase();
  const route = remote.route || remote.share_url || '';
  const modalElement = document.getElementById('remote-delete-modal');
  if (!modalElement || typeof bootstrap === 'undefined' || !bootstrap.Modal) {
    let fallbackMessage = `¿Seguro que querés eliminar el remote "${remote.name}"? Esta acción no se puede deshacer.`;
    if (remoteType === 'local') {
      fallbackMessage += route
        ? ` Se eliminará la carpeta ${route} con todos sus archivos.`
        : ' Se eliminarán los archivos locales asociados.';
      const admin = getAdminEmail();
      if (admin) {
        fallbackMessage += ` Si necesitás conservarlos contactá al administrador en ${admin}.`;
      }
    }
    const confirmed = window.confirm(fallbackMessage);
    if (!confirmed) {
      return;
    }
    handleRemoteDelete(remote);
    return;
  }
  if (!deleteModalInstance) {
    deleteModalInstance = new bootstrap.Modal(modalElement);
  }
  pendingRemoteDeletion = { ...remote };
  const nameTarget = document.getElementById('remote-delete-name');
  if (nameTarget) {
    nameTarget.textContent = remote.name;
  }
  const impactTarget = document.getElementById('remote-delete-impact');
  if (impactTarget) {
    if (remoteType === 'local') {
      impactTarget.textContent =
        'Esta acción borrará la configuración y todos los archivos guardados en su carpeta local.';
    } else {
      impactTarget.textContent =
        'Esta acción borrará la configuración almacenada en el orquestador.';
    }
  }
  const routeTarget = document.getElementById('remote-delete-route');
  const routeWrapper = document.getElementById('remote-delete-route-wrapper');
  if (routeTarget) {
    if (route && remoteType === 'local') {
      routeTarget.textContent = route;
      if (routeWrapper) {
        routeWrapper.classList.remove('d-none');
      }
    } else {
      routeTarget.textContent = '';
      if (routeWrapper) {
        routeWrapper.classList.add('d-none');
      }
    }
  }
  const emailValue = getAdminEmail();
  const emailLink = document.getElementById('remote-delete-admin-email');
  const emailWrapper = document.getElementById('remote-delete-admin-wrapper');
  if (emailLink) {
    if (emailValue) {
      emailLink.textContent = emailValue;
      emailLink.href = `mailto:${emailValue}`;
      if (emailWrapper) {
        emailWrapper.classList.remove('d-none');
      }
    } else {
      emailLink.textContent = '';
      emailLink.removeAttribute('href');
      if (emailWrapper) {
        emailWrapper.classList.add('d-none');
      }
    }
  }
  deleteModalInstance.show();
}

async function handleRemoteDelete(remote) {
  if (!remote || !remote.name) {
    return;
  }
  const remoteType = (remote.type || '').toLowerCase();
  const overlayMessage =
    remoteType === 'local'
      ? 'Eliminando el remote y su carpeta local…'
      : 'Eliminando remote…';
  toggleRemoteOverlay(true, overlayMessage);
  try {
    const resp = await fetch(`/rclone/remotes/${encodeURIComponent(remote.name)}`, {
      method: 'DELETE',
    });
    if (resp.status === 401) {
      window.location.href = '/login';
      return;
    }
    const data = await resp.json().catch(() => ({}));
    if (resp.ok) {
      if (editingRemote && editingRemote.name === remote.name) {
        exitRemoteEditMode({ resetForm: true });
      }
      let successMessage = 'Remote eliminado correctamente.';
      const removedPath =
        data && typeof data.removed_path === 'string' ? data.removed_path.trim() : '';
      if (removedPath) {
        successMessage = `Remote eliminado correctamente. Se borró la carpeta ${removedPath}.`;
      } else if (remoteType === 'local') {
        successMessage =
          'Remote eliminado correctamente. Se eliminaron los archivos locales asociados.';
      }
      showFeedback(successMessage, 'success');
      loadRemotes();
    } else {
      const message = data && data.error ? data.error : 'No se pudo eliminar el remote.';
      showFeedback(message, 'danger');
    }
  } catch (error) {
    showFeedback('Ocurrió un error al comunicarse con el servidor.', 'danger');
  } finally {
    toggleRemoteOverlay(false);
  }
}

function resetPanels() {
  document.querySelectorAll('[data-remote-panel]').forEach((panel) => {
    panel.classList.add('d-none');
  });
  updateLocalSummary();
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
  if (editingRemote && editingRemote.type === 'local') {
    const storedRoute = editingRemote.route || editingRemote.share_url || '';
    const parentPath = getLocalParentPath(storedRoute);
    if (parentPath) {
      select.value = parentPath;
    }
  }
  updateLocalSummary();
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
  const nameInput = document.getElementById('remote_name');
  if (nameInput) {
    nameInput.addEventListener('input', () => {
      updateLocalSummary();
    });
  }
  const localPathSelect = document.getElementById('local_path');
  if (localPathSelect) {
    localPathSelect.addEventListener('change', () => {
      updateLocalSummary();
    });
  }
  const cancelButton = document.getElementById('remote-cancel-edit');
  if (cancelButton) {
    cancelButton.addEventListener('click', () => {
      exitRemoteEditMode({ resetForm: true });
    });
  }
  const typeSelect = document.getElementById('remote_type');
  if (typeSelect) {
    showPanelForType(typeSelect.value);
    typeSelect.addEventListener('change', (event) => {
      const selected = event.target.value;
      showFeedback('', 'info');
      showPanelForType(selected);
      updateLocalSummary();
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
  form.addEventListener('reset', () => {
    window.setTimeout(() => {
      updateLocalSummary();
    }, 0);
  });
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    showFeedback('', 'info');
    const type = typeSelect ? typeSelect.value : '';
    const name = nameInput ? nameInput.value.trim() : '';
    const isEditing = Boolean(editingRemote && editingRemote.name);
    if (!name) {
      showFeedback('Completá un nombre para el remote.', 'danger');
      return;
    }
    if (!type) {
      showFeedback('Elegí el tipo de remote que querés configurar.', 'danger');
      return;
    }

    const payload = {
      name,
      type,
      settings: {},
    };
    let overlayMessage = isEditing ? 'Actualizando remote…' : 'Guardando remote…';
    if (type === 'local') {
      const select = document.getElementById('local_path');
      const value = select ? select.value.trim() : '';
      if (!value) {
        showFeedback('Seleccioná la carpeta local donde guardar los respaldos.', 'danger');
        return;
      }
      payload.settings.path = value;
      overlayMessage = isEditing
        ? 'Actualizando remote local…'
        : 'Preparando carpeta local…';
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
        payload.settings.folder_name = name;
        overlayMessage = isEditing
          ? 'Actualizando carpeta y enlace en Google Drive…'
          : 'Creando carpeta y generando enlace en Google Drive…';
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
    toggleRemoteOverlay(true, overlayMessage);
    try {
      const endpoint =
        isEditing && editingRemote
          ? `/rclone/remotes/${encodeURIComponent(editingRemote.name)}`
          : '/rclone/remotes';
      const resp = await fetch(endpoint, {
        method: isEditing ? 'PUT' : 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      if (resp.status === 401) {
        window.location.href = '/login';
        return;
      }
      const data = await resp.json().catch(() => ({}));
      if (resp.ok) {
        if (isEditing) {
          exitRemoteEditMode({ resetForm: false });
        }
        form.reset();
        resetPanels();
        driveValidation = { status: 'idle', token: '' };
        updateDriveFeedback('', 'muted');
        delete directoryCache.local;
        resetSftpBrowser(true);
        const feedbackOptions = {};
        const linkValue = (data && typeof data.share_url === 'string' && data.share_url.trim())
          ? data.share_url.trim()
          : (data && typeof data.route === 'string' && data.route.trim())
            ? data.route.trim()
            : '';
        const successBase = isEditing
          ? 'Remote actualizado correctamente.'
          : 'Remote guardado correctamente.';
        let successMessage = successBase;
        if (linkValue) {
          const looksLikeUrl = /^https?:\/\//i.test(linkValue);
          if (type === 'drive' && looksLikeUrl) {
            feedbackOptions.link = linkValue;
            successMessage = `${successBase} Compartí este enlace:`;
          } else {
            successMessage = `${successBase} Ruta configurada: ${linkValue}`;
          }
        }
        showFeedback(successMessage, 'success', feedbackOptions);
        loadRemotes();
      } else {
        const message = data && data.error ? data.error : 'No se pudo guardar el remote';
        showFeedback(message, 'danger');
      }
    } catch (err) {
      showFeedback('Ocurrió un error al comunicarse con el servidor.', 'danger');
    } finally {
      if (submitBtn) {
        submitBtn.disabled = false;
      }
      toggleRemoteOverlay(false);
    }
  });
  updateLocalSummary();
}

document.addEventListener('DOMContentLoaded', () => {
  initDeleteModal();
  initRemoteForm();
  loadRemotes();
  updateLocalSummary();
});
