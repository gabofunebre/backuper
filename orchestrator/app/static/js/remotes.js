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
});
