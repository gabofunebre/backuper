let currentAuthSessionId = null;

async function loadRemotes() {
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
  const authSelect = document.getElementById('auth_remote');
  if (authSelect) {
    authSelect.innerHTML = '<option value=""></option>';
  }
  remotes.forEach(name => {
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
    if (authSelect) {
      const opt2 = document.createElement('option');
      opt2.value = name;
      opt2.textContent = name;
      authSelect.appendChild(opt2);
    }
  });
}

document.addEventListener('DOMContentLoaded', () => {
  loadRemotes();
  const form = document.getElementById('remote-form');
  if (form) {
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const payload = {
        name: document.getElementById('remote_name').value,
        type: document.getElementById('remote_type').value,
      };
      const resp = await fetch('/rclone/remotes', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      if (resp.status === 401) {
        window.location.href = '/login';
        return;
      }
      if (resp.ok) {
        form.reset();
        loadRemotes();
      }
    });
  }

  const startBtn = document.getElementById('start-auth');
  if (startBtn) {
    startBtn.addEventListener('click', async () => {
      const name = document.getElementById('auth_remote').value;
      if (!name) return;
      const resp = await fetch(`/rclone/remotes/${name}/authorize`);
      if (resp.status === 401) {
        window.location.href = '/login';
        return;
      }
      const data = await resp.json().catch(() => ({}));
      if (resp.ok && data.url) {
        const urlInput = document.getElementById('auth_url');
        const link = document.getElementById('auth_link');
        const container = document.getElementById('auth-url-container');
        if (urlInput) urlInput.value = data.url;
        if (link) {
          link.href = data.url;
          link.textContent = data.url;
        }
        if (container) container.style.display = '';
        currentAuthSessionId = data.session_id || null;
        const codeInput = document.getElementById('auth_code');
        if (codeInput) codeInput.value = '';
      } else {
        if (container) container.style.display = 'none';
        currentAuthSessionId = null;
        const message = data && data.error ? data.error : 'No se pudo iniciar la autorización';
        alert(message);
      }
    });
  }

  const copyBtn = document.getElementById('copy-auth-url');
  if (copyBtn) {
    copyBtn.addEventListener('click', () => {
      const input = document.getElementById('auth_url');
      if (!input) return;
      navigator.clipboard.writeText(input.value);
    });
  }

  const finishBtn = document.getElementById('finish-auth');
  if (finishBtn) {
    finishBtn.addEventListener('click', async () => {
      const name = document.getElementById('auth_remote').value;
      const codeInput = document.getElementById('auth_code');
      const code = codeInput ? codeInput.value : '';
      if (!name || !code) return;
      if (!currentAuthSessionId) {
        alert('Inicie la autorización antes de completarla.');
        return;
      }
      const resp = await fetch(`/rclone/remotes/${name}/authorize`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: currentAuthSessionId, code })
      });
      if (resp.status === 401) {
        window.location.href = '/login';
        return;
      }
      const data = await resp.json().catch(() => ({}));
      if (resp.ok) {
        if (codeInput) codeInput.value = '';
        currentAuthSessionId = null;
        alert('Remote authorized');
      } else {
        const message = data && data.error ? data.error : 'No se pudo completar la autorización';
        alert(message);
      }
    });
  }
});
