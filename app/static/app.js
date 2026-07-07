/* ==========================================================================
   AutoPay AI — Single-Page Application (vanilla JS)
   ========================================================================== */

// ── API Client ──────────────────────────────────────────────────────────
const API = (() => {
  const BASE = "/api/v1";
  let _accessToken = localStorage.getItem("access_token") || "";
  let _refreshToken = localStorage.getItem("refresh_token") || "";

  function setTokens({ access_token, refresh_token, expires_in }) {
    _accessToken = access_token;
    _refreshToken = refresh_token;
    localStorage.setItem("access_token", access_token);
    localStorage.setItem("refresh_token", refresh_token);
    localStorage.setItem("token_expires", Date.now() + expires_in * 1000);
  }

  function clearTokens() {
    _accessToken = "";
    _refreshToken = "";
    localStorage.removeItem("access_token");
    localStorage.removeItem("refresh_token");
    localStorage.removeItem("token_expires");
  }

  function isLoggedIn() {
    const exp = localStorage.getItem("token_expires");
    return _accessToken && exp && Date.now() < Number(exp);
  }

  async function request(method, path, body = null, auth = true) {
    const headers = { "Content-Type": "application/json" };
    if (auth && _accessToken) headers["Authorization"] = `Bearer ${_accessToken}`;

    const opts = { method, headers };
    if (body !== null) opts.body = JSON.stringify(body);

    const res = await fetch(`${BASE}${path}`, opts);

    if (res.status === 401 && auth && _refreshToken) {
      const refreshed = await refreshTokens();
      if (refreshed) return request(method, path, body, auth);
      clearTokens();
      routeTo("/login");
      throw new Error("Session expired. Please log in again.");
    }

    if (res.status === 204) return null;

    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const msg = data.detail || data.message || JSON.stringify(data);
      throw new Error(msg);
    }
    return data;
  }

  async function refreshTokens() {
    try {
      const res = await fetch(`${BASE}/auth/refresh`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token: _refreshToken }),
      });
      if (!res.ok) return false;
      setTokens(await res.json());
      return true;
    } catch {
      return false;
    }
  }

  async function uploadBill(file, text) {
    const fd = new FormData();
    if (file) fd.append("file", file);
    if (text) fd.append("request_bill", text);

    const res = await fetch(`${BASE}/bills/upload`, {
      method: "POST",
      headers: authHeader(),
      body: fd,
    });

    if (res.status === 401) {
      const refreshed = await refreshTokens();
      if (refreshed) return uploadBill(file, text);
      clearTokens();
      routeTo("/login");
      throw new Error("Session expired.");
    }

    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || "Upload failed");
    return data;
  }

  function authHeader() {
    return _accessToken ? { Authorization: `Bearer ${_accessToken}` } : {};
  }

  return {
    setTokens,
    clearTokens,
    isLoggedIn,
    request,
    uploadBill,

    auth: {
      signup: (d) => request("POST", "/auth/signup", d, false),
      login: (d) => request("POST", "/auth/login", d, false),
      refresh: (d) => request("POST", "/auth/refresh", d, false),
      logout: () => request("POST", "/auth/logout", { refresh_token: _refreshToken }).finally(clearTokens),
      me: () => request("GET", "/auth/me"),
      wallet: () => request("GET", "/auth/wallet"),
      linkCode: () => request("POST", "/auth/telegram/link-code"),
      deleteLinkCode: () => request("DELETE", "/auth/telegram/link-code"),
      unlinkTelegram: () => request("DELETE", "/auth/telegram/link"),
    },

    bills: {
      list: (status) => request("GET", `/bills${status ? `?status_filter=${status}` : ""}`),
      get: (id) => request("GET", `/bills/${id}`),
      create: (d) => request("POST", "/bills", d),
      pay: (id) => request("POST", `/bills/${id}/pay`),
      cancel: (id) => request("POST", `/bills/${id}/cancel`),
    },

    kyc: {
      submitBvn: (bvn) => request("POST", "/kyc/bvn", { bvn }),
      getBvn: () => request("GET", "/kyc/bvn"),
    },

    wallet: {
      topup: (amount, callback_url) => request("POST", "/wallet/topup", { amount, callback_url }),
      verifyTopup: (reference) => request("POST", "/wallet/topup/verify", { reference }),
      transactions: (limit, type) => request("GET", `/wallet/transactions?limit=${limit || 20}${type ? `&type=${type}` : ""}`),
      getVirtualAccount: () => request("GET", "/wallet/virtual-account"),
      provisionVirtualAccount: () => request("POST", "/wallet/provision"),
    },
  };
})();

// ── Router ───────────────────────────────────────────────────────────────
function routeTo(path) {
  window.location.hash = path;
}

function getRoute() {
  const hash = window.location.hash.replace("#", "") || "/login";
  return hash;
}

window.addEventListener("hashchange", render);

// ── Toast ────────────────────────────────────────────────────────────────
function toast(message, type = "info") {
  const el = document.getElementById("toast");
  el.textContent = message;
  el.className = `toast show ${type}`;
  clearTimeout(el._timer);
  el._timer = setTimeout(() => { el.className = "toast"; }, 3500);
}

// ── Formatters ────────────────────────────────────────────────────────────
function fmtMoney(val) {
  const n = parseFloat(val) || 0;
  return "\u20A6" + n.toLocaleString("en-NG", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function fmtDate(iso) {
  if (!iso) return "\u2014";
  const d = new Date(iso);
  return d.toLocaleDateString("en-NG", { year: "numeric", month: "short", day: "numeric" });
}

function fmtDateFull(iso) {
  if (!iso) return "\u2014";
  const d = new Date(iso);
  return d.toLocaleString("en-NG", { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function statusBadge(status) {
  const map = {
    pending: "badge-pending", paid: "badge-paid", processing: "badge-processing",
    failed: "badge-failed", cancelled: "badge-cancelled", scheduled: "badge-scheduled",
    hold: "badge-hold", pay_now: "badge-approve", schedule: "badge-schedule",
    success: "badge-paid", reversed: "badge-hold", credit: "badge-approve", debit: "badge-hold",
  };
  const cls = map[status] || "badge-pending";
  return `<span class="badge ${cls}">${status.replace(/_/g, " ")}</span>`;
}

// ── Nav ──────────────────────────────────────────────────────────────────
function updateNav() {
  const nav = document.getElementById("topnav");
  const userSpan = document.getElementById("nav-user");
  const tgDot = document.getElementById("nav-tg-dot");

  if (API.isLoggedIn()) {
    nav.classList.remove("hidden");
    API.auth.me().then((u) => {
      userSpan.textContent = u.email;
      tgDot.className = u.is_telegram_linked ? "dot dot-on" : "dot dot-off";
      tgDot.title = u.is_telegram_linked ? "Telegram linked" : "Telegram not linked";
    }).catch(() => {});
  } else {
    nav.classList.add("hidden");
  }
}

function highlightNav() {
  const route = getRoute();
  document.querySelectorAll(".nav-item").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.route === route);
  });
}

// ── Modal ────────────────────────────────────────────────────────────────
function showModal(title, body, actions) {
  let overlay = document.getElementById("modal-overlay");
  if (overlay) overlay.remove();

  overlay = document.createElement("div");
  overlay.id = "modal-overlay";
  overlay.className = "modal-backdrop";
  overlay.innerHTML = `
    <div class="modal">
      <div class="modal-title">${title}</div>
      <div class="modal-body">${body}</div>
      <div class="modal-actions" id="modal-actions"></div>
    </div>`;
  document.body.appendChild(overlay);

  const actionsEl = document.getElementById("modal-actions");
  actions.forEach((a) => {
    const btn = document.createElement("button");
    btn.className = a.cls || "btn-outline";
    btn.textContent = a.label;
    btn.onclick = () => { overlay.remove(); if (a.fn) a.fn(); };
    actionsEl.appendChild(btn);
  });
}

function closeModal() {
  const overlay = document.getElementById("modal-overlay");
  if (overlay) overlay.remove();
}

// ── Page: Login / Signup ─────────────────────────────────────────────────
function renderAuthPage(mode) {
  const isSignup = mode === "signup";
  return `
    <div class="auth-shell">
      <div class="auth-side">
        <div>
          <div class="font-syne text-4xl font-bold tracking-tight"><span class="text-lime">\u2B21</span> AutoPay<span class="text-lime">AI</span></div>
          <p class="text-mute text-sm mt-4">AI-powered bill automation for Nigeria. Send a bill \u2014 we decide when to pay it.</p>
        </div>
        <div class="text-xs text-mute">&copy; ${new Date().getFullYear()} AutoPay AI</div>
      </div>
      <div class="auth-main">
        <div class="max-w-md w-full">
          <h1 class="font-syne text-3xl font-bold tracking-tight mb-2">${isSignup ? "Create account" : "Welcome back"}</h1>
          <p class="text-mute text-sm mb-8">${isSignup ? "Sign up to start automating your bills." : "Log in to your AutoPay AI account."}</p>

          <form id="auth-form" class="space-y-4">
            ${isSignup ? `
            <div class="flex gap-3">
              <div class="flex-1">
                <label class="label">First name</label>
                <input class="input" name="first_name" required placeholder="Ada" />
              </div>
              <div class="flex-1">
                <label class="label">Last name</label>
                <input class="input" name="last_name" required placeholder="Lovelace" />
              </div>
            </div>` : ""}
            <div>
              <label class="label">Email</label>
              <input class="input" name="email" type="email" required placeholder="you@example.com" />
            </div>
            ${isSignup ? `
            <div>
              <label class="label">Phone number</label>
              <input class="input" name="phone_number" required placeholder="08012345678" />
            </div>` : ""}
            <div>
              <label class="label">Password${isSignup ? " (8+ chars, letter + digit)" : ""}</label>
              <input class="input" name="password" type="password" required placeholder="${isSignup ? "Min 8 chars" : "\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022"}" />
            </div>
            <button type="submit" class="btn-lime w-full">${isSignup ? "Create account" : "Log in"}</button>
          </form>

          <p class="text-sm text-mute mt-6 text-center">
            ${isSignup
              ? `Already have an account? <a href="#/login" class="text-lime">Log in</a>`
              : `Don\u2019t have an account? <a href="#/signup" class="text-lime">Sign up</a>`}
          </p>
        </div>
      </div>
    </div>`;
}

async function handleAuthSubmit(e, mode) {
  e.preventDefault();
  const form = e.target;
  const data = Object.fromEntries(new FormData(form));
  const btn = form.querySelector("button[type=submit]");
  btn.disabled = true;
  btn.textContent = "Please wait\u2026";

  try {
    const result = mode === "signup"
      ? await API.auth.signup(data)
      : await API.auth.login({ email: data.email, password: data.password });

    API.setTokens(result);
    toast(mode === "signup" ? "Account created!" : "Logged in!", "success");
    updateNav();
    routeTo("/dashboard");
  } catch (err) {
    toast(err.message, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = mode === "signup" ? "Create account" : "Log in";
  }
}

// ── Page: Dashboard ──────────────────────────────────────────────────────
function renderDashboard() {
   return `
     <div class="page-content fade-up">
       <div class="page-header">
         <div class="page-eyebrow">OVERVIEW</div>
         <h1 class="page-title">Dashboard</h1>
       </div>
       <div id="dash-stats" class="grid grid-cols-1 sm:grid-cols-3 gap-4 mb-8">
         <div class="card stat-card p-6"><div class="label">Balance</div><div id="dash-balance" class="text-3xl font-bold mt-1 text-lime">\u2026</div></div>
         <div class="card stat-card p-6"><div class="label">Pending bills</div><div id="dash-pending" class="text-3xl font-bold mt-1">\u2026</div></div>
         <div class="card stat-card p-6"><div class="label">Total paid</div><div id="dash-paid" class="text-3xl font-bold mt-1">\u2026</div></div>
       </div>
       <div class="grid grid-cols-1 md:grid-cols-2 gap-6 mb-8">
         <div class="card">
           <div class="flex justify-between items-center mb-4">
             <h2 class="font-syne text-lg font-bold">Recent bills</h2>
             <button class="btn-ghost" onclick="routeTo('/bills')">View all \u2192</button>
           </div>
           <div id="dash-bills"></div>
         </div>
         <div class="card">
           <div class="flex justify-between items-center mb-4">
             <h2 class="font-syne text-lg font-bold">Scheduled bills</h2>
             <button class="btn-ghost" onclick="routeTo('/schedule')">View all \u2192</button>
           </div>
           <div id="dash-bills-scheduled"></div>
         </div>
       </div>
     </div>`;
 }

async function loadDashboard() {
  try {
    const [wallet, bills, scheduledBills] = await Promise.all([
      API.auth.wallet(),
      API.bills.list(),
      API.bills.list("scheduled"),
    ]);

    document.getElementById("dash-balance").textContent = fmtMoney(wallet.balance);

    const pending = bills.filter((b) => b.status === "pending" || b.status === "scheduled");
    const paid = bills.filter((b) => b.status === "paid");
    document.getElementById("dash-pending").textContent = pending.length;
    document.getElementById("dash-paid").textContent = paid.length;

    const recent = bills.slice(0, 5);
    const container = document.getElementById("dash-bills");
    if (recent.length === 0) {
      container.innerHTML = `<div class="empty"><div class="icon">\uD83D\uDCB8</div><p>No bills yet. Upload one to get started.</p></div>`;
    } else {
      container.innerHTML = `<table class="table"><thead><tr><th>Vendor</th><th>Amount</th><th>Due</th><th>Status</th></tr></thead><tbody>${recent.map((b) => `<tr><td>${esc(b.vendor_name)}</td><td>${fmtMoney(b.amount)}</td><td>${fmtDate(b.due_date)}</td><td>${statusBadge(b.status)}</td></tr>`).join("")}</tbody></table>`;
    }

    // Show latest scheduled bills in dashboard, max 2
    const containerScheduled = document.getElementById("dash-bills-scheduled");
    if (scheduledBills.length === 0) {
      containerScheduled.innerHTML = `<div class="empty"><div class="icon">\uD83D\uDCC5</div><p>No scheduled bills.</p></div>`;
    } else {
      const latest = scheduledBills.slice(0, 2);
      containerScheduled.innerHTML = `
        <table class="table">
          <thead><tr><th>Vendor</th><th>Amount</th><th>First due</th><th>Status</th></tr></thead>
          <tbody>${latest.map((b) => `<tr><td>${esc(b.vendor_name)}</td><td>${fmtMoney(b.amount)}</td><td>${fmtDate(b.due_date)}</td><td>${statusBadge(b.status)}</td></tr>`).join("")}</tbody>
        </table>
        ${scheduledBills.length > 2 ? `<div class="text-center mt-4"><button class="btn-outline text-sm" onclick="routeTo('/schedule')">View all scheduled \u2192</button></div>` : ""}`;
    }
  } catch (err) {
    toast(err.message, "error");
  }
}

// ── Page: Bills ──────────────────────────────────────────────────────────
function renderBills() {
  return `
    <div class="page-content fade-up">
      <div class="page-header">
        <div class="page-eyebrow">BILLS</div>
        <div class="flex items-center justify-between">
          <h1 class="page-title">Your bills</h1>
          <button class="btn-lime" onclick="routeTo('/upload')">+ Upload bill</button>
        </div>
      </div>
      <div class="flex gap-2 mb-6" id="bill-filters">
        <button class="nav-item active" data-filter="" onclick="filterBills('', this)">All</button>
        <button class="nav-item" data-filter="pending" onclick="filterBills('pending', this)">Pending</button>
        <button class="nav-item" data-filter="scheduled" onclick="filterBills('scheduled', this)">Scheduled</button>
        <button class="nav-item" data-filter="paid" onclick="filterBills('paid', this)">Paid</button>
        <button class="nav-item" data-filter="cancelled" onclick="filterBills('cancelled', this)">Cancelled</button>
      </div>
      <div id="bills-list" class="card p-4"></div>
    </div>`;
}

async function loadBills(statusFilter = "") {
  const container = document.getElementById("bills-list");
  try {
    const bills = await API.bills.list(statusFilter || undefined);
    if (bills.length === 0) {
      container.innerHTML = `<div class="empty"><div class="icon">\uD83D\uDCB8</div><p>No bills found.</p></div>`;
      return;
    }
    container.innerHTML = `<table class="table"><thead><tr><th>Vendor</th><th>Amount</th><th>Due</th><th>Status</th><th>Actions</th></tr></thead><tbody>${bills.map((b) => billRow(b)).join("")}</tbody></table>`;
  } catch (err) {
    container.innerHTML = `<div class="empty text-red">${esc(err.message)}</div>`;
  }
}

function billRow(b) {
  const isPending = b.status === "pending" || b.status === "scheduled" || b.status === "hold";
  const isCancellable = b.status === "pending" || b.status === "scheduled";
  return `<tr>
    <td>${esc(b.vendor_name)}</td>
    <td>${fmtMoney(b.amount)}</td>
    <td>${fmtDate(b.due_date)}</td>
    <td>${statusBadge(b.status)}</td>
    <td class="flex gap-2">
      ${isPending ? `<button class="btn-lime text-xs py-1 px-3" onclick="payBill(${b.id})">Pay now</button>` : ""}
      ${isCancellable ? `<button class="btn-danger text-xs py-1" onclick="cancelBill(${b.id})">Cancel</button>` : ""}
    </td></tr>`;
}

function filterBills(status, btn) {
  document.querySelectorAll("#bill-filters .nav-item").forEach((b) => b.classList.remove("active"));
  btn.classList.add("active");
  loadBills(status);
}

async function payBill(id) {
  showModal("Confirm payment", `Pay bill #${id} now from your wallet balance?`, [
    { label: "Cancel", cls: "btn-outline" },
    { label: "Pay now", cls: "btn-lime", fn: async () => {
      try {
        const res = await API.bills.pay(id);
        toast(res.message || "Bill paid!", "success");
        loadBills();
      } catch (err) { toast(err.message, "error"); }
    }},
  ]);
}

async function cancelBill(id) {
  showModal("Cancel bill", `Are you sure you want to cancel bill #${id}?`, [
    { label: "Keep", cls: "btn-outline" },
    { label: "Cancel bill", cls: "btn-danger", fn: async () => {
      try {
        const res = await API.bills.cancel(id);
        toast(res.message || "Bill cancelled.", "success");
        loadBills();
      } catch (err) { toast(err.message, "error"); }
    }},
  ]);
}

// ── Page: Upload ─────────────────────────────────────────────────────────
function renderUpload() {
  return `
    <div class="page-content fade-up">
      <div class="page-header">
        <div class="page-eyebrow">UPLOAD</div>
        <h1 class="page-title">Upload a bill</h1>
        <p class="page-subtitle">Upload a photo, PDF, or paste bill text. Our AI will extract the details.</p>
      </div>

      <div id="drop-zone" class="mb-6">
        <div class="text-4xl mb-4">\uD83D\uDCF4</div>
        <p class="text-lg font-bold">Drop a file here or click to browse</p>
        <p class="text-mute text-sm mt-2">PDF, PNG, JPG accepted</p>
        <input type="file" id="file-input" accept=".pdf,.png,.jpg,.jpeg" class="hidden" />
      </div>

      <div class="text-center text-mute text-sm mb-6">\u2014 or paste the bill text below \u2014</div>

      <div class="max-w-2xl">
        <label class="label">Bill text</label>
        <textarea id="bill-text" class="input" rows="5" placeholder="Paste the full bill text here\u2026"></textarea>
        <div class="flex justify-end gap-3 mt-4">
          <button class="btn-outline" onclick="routeTo('/bills')">Cancel</button>
          <button class="btn-lime" id="upload-btn" onclick="handleUpload()">Upload &amp; process</button>
        </div>
      </div>

      <div id="upload-result" class="mt-6"></div>
    </div>`;
}

function initUpload() {
  const dropZone = document.getElementById("drop-zone");
  const fileInput = document.getElementById("file-input");
  if (!dropZone || !fileInput) return;

  dropZone.addEventListener("click", () => fileInput.click());
  dropZone.addEventListener("dragover", (e) => { e.preventDefault(); dropZone.classList.add("drag-over"); });
  dropZone.addEventListener("dragleave", () => dropZone.classList.remove("drag-over"));
  dropZone.addEventListener("drop", (e) => {
    e.preventDefault();
    dropZone.classList.remove("drag-over");
    fileInput.files = e.dataTransfer.files;
    const fn = e.dataTransfer.files[0]?.name;
    dropZone.innerHTML = `<div class="text-4xl mb-2">\uD83D\uDCC4</div><p class="font-bold">${esc(fn || "File selected")}</p>`;
  });
  fileInput.addEventListener("change", () => {
    const fn = fileInput.files[0]?.name;
    if (fn) dropZone.innerHTML = `<div class="text-4xl mb-2">\uD83D\uDCC4</div><p class="font-bold">${esc(fn)}</p>`;
  });
}

async function handleUpload() {
  const fileInput = document.getElementById("file-input");
  const textInput = document.getElementById("bill-text");
  const btn = document.getElementById("upload-btn");
  const resultDiv = document.getElementById("upload-result");

  const file = fileInput?.files[0] || null;
  const text = textInput?.value?.trim() || null;

  if (!file && !text) {
    toast("Provide a file or bill text.", "error");
    return;
  }

  btn.disabled = true;
  btn.textContent = "Processing\u2026";

  try {
    const res = await API.uploadBill(file, text);
    const b = res.bill;
    resultDiv.innerHTML = `
      <div class="card p-6 fade-up">
        <div class="flex items-center justify-between mb-4">
          <h3 class="font-syne text-lg font-bold">${esc(b.vendor_name)}</h3>
          ${statusBadge(b.status)}
        </div>
        <div class="grid grid-cols-2 gap-4 mb-4">
          <div><div class="label">Amount</div><div class="text-xl font-bold">${fmtMoney(b.amount)}</div></div>
          <div><div class="label">Due date</div><div>${fmtDate(b.due_date)}</div></div>
        </div>
        ${res.decision ? `
        <div class="card-2 p-4 mt-2">
          <div class="label mb-1">Agent decision</div>
          <div class="flex items-center gap-3">
            ${statusBadge(res.decision)}
            <span class="text-sm text-mute">${esc(res.decision_reason || "")}</span>
          </div>
        </div>` : ""}
        ${res.message ? `<p class="text-sm text-mute mt-3">${esc(res.message)}</p>` : ""}
      </div>`;
    toast(res.message || "Bill uploaded!", "success");
  } catch (err) {
    toast(err.message, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = "Upload & process";
  }
}

// ── Page: Schedule ───────────────────────────────────────────────────────
function renderSchedule() {
  return `
    <div class="page-content fade-up">
      <div class="page-header">
        <div class="page-eyebrow">SCHEDULE</div>
        <h1 class="page-title">Scheduled bills</h1>
        <p class="page-subtitle">These bills are waiting for their due date.</p>
      </div>
      <div id="schedule-list"></div>
    </div>`;
}

async function loadSchedule() {
  const container = document.getElementById("schedule-list");
  try {
    const bills = await API.bills.list("scheduled");
    if (bills.length === 0) {
      container.innerHTML = `<div class="empty"><div class="icon">\uD83D\uDCC5</div><p>No scheduled bills.</p></div>`;
      return;
    }
    container.innerHTML = `<div class="card"><table class="table"><thead><tr><th>Vendor</th><th>Amount</th><th>Due</th><th>Status</th><th>Actions</th></tr></thead><tbody>${bills.map(billRow).join("")}</tbody></table></div>`;
  } catch (err) {
    container.innerHTML = `<div class="empty text-red">${esc(err.message)}</div>`;
  }
}

// ── Page: Wallet ─────────────────────────────────────────────────────────
function renderWallet() {
  return `
    <div class="page-content fade-up">
      <div class="page-header">
        <div class="page-eyebrow">WALLET</div>
        <h1 class="page-title">Your wallet</h1>
      </div>

      <div class="card stat-card p-6 mb-8 max-w-xs">
        <div class="label">Available balance</div>
        <div id="wallet-balance" class="text-3xl font-bold mt-1 text-lime">\u2026</div>
      </div>

      <div class="card p-6 mb-6">
        <h3 class="font-syne font-bold mb-4">Virtual account</h3>
        <div id="virtual-account-section">
          <div class="empty"><div class="icon">\uD83D\uDC65</div><p>Loading\u2026</p></div>
        </div>
      </div>

      <div class="card p-6 mb-6">
        <h3 class="font-syne font-bold mb-4">Schedule bills</h3>
        <div class="flex gap-3 items-end">
          <div class="flex-1">
            <p class="text-mute text-sm">Set up future recurring or one-time bills that auto-pay from your wallet balance.</p>
          </div>
          <button class="btn-lime" id="schedule-bills-btn" onclick="routeTo('/schedule')">Schedule bill</button>
        </div>
      </div>

      <div class="card p-6 mb-6">
        <h3 class="font-syne font-bold mb-4">Top up wallet</h3>
        <div class="flex gap-3 items-end">
          <div class="flex-1">
            <label class="label">Amount (NGN)</label>
            <input class="input" id="topup-amount" type="number" min="100" placeholder="5000" />
          </div>
          <button class="btn-lime" id="topup-btn" onclick="handleTopup()">Top up</button>
        </div>
        <p class="text-xs text-mute mt-2">Min \u20A6100 \u2014 You\u2019ll be redirected to Nomba Checkout to complete payment.</p>
      </div>

      <div class="card p-6">
        <div class="flex justify-between items-center mb-4">
          <h3 class="font-syne font-bold">Transactions</h3>
          <div class="flex gap-2">
            <button class="nav-item active" onclick="loadTransactions('')">All</button>
            <button class="nav-item" onclick="loadTransactions('credit')">Credits</button>
            <button class="nav-item" onclick="loadTransactions('debit')">Debits</button>
          </div>
        </div>
        <div id="wallet-transactions"></div>
      </div>
    </div>`;
}

async function loadWallet() {
  try {
    const [wallet, txns, va] = await Promise.all([
      API.auth.wallet(),
      API.wallet.transactions(20),
      API.wallet.getVirtualAccount(),
    ]);

    document.getElementById("wallet-balance").textContent = fmtMoney(wallet.balance);
    renderVirtualAccount(va);
    renderTransactions(txns);
    // If there's a pending top-up in sessionStorage (the user came
    // back from the provider's Checkout page and is checking the
    // wallet), re-render the "I've paid" card so they can verify.
    const pendingRef = sessionStorage.getItem("pending_topup_ref");
    const pendingAmount = sessionStorage.getItem("pending_topup_amount");
    if (pendingRef && pendingAmount) {
      renderPendingTopupCard(pendingRef, pendingAmount);
    }
  } catch (err) {
    toast(err.message, "error");
  }
}

function renderVirtualAccount(va) {
  const container = document.getElementById("virtual-account-section");
  if (!container) return;

  if (!va) {
    container.innerHTML = `
      <div class="empty">
        <div class="icon">\uD83D\uDC65</div>
        <p>You don't have a virtual account yet.</p>
        <p class="text-xs text-mute mt-1">A dedicated Nombank account number lets anyone send you money by bank transfer \u2014 no checkout page needed.</p>
        <button class="btn-lime mt-4" id="provision-va-btn" onclick="handleProvision()">Provision virtual account</button>
      </div>`;
    return;
  }

  container.innerHTML = `
    <div class="grid gap-4" style="grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));">
      <div>
        <div class="label">Account number</div>
        <div class="text-lg font-mono font-bold mt-1">${esc(va.account_number || "\u2014")}</div>
      </div>
      <div>
        <div class="label">Account name</div>
        <div class="text-lg font-bold mt-1">${esc(va.account_name || "\u2014")}</div>
      </div>
      <div>
        <div class="label">Bank</div>
        <div class="text-lg font-bold mt-1">${esc(va.bank_name || "\u2014")}</div>
      </div>
    </div>
    <p class="text-xs text-mute mt-4">Share this account number to receive payments via bank transfer. Funds are automatically credited to your AutoPay wallet.</p>`;
}

async function handleProvision() {
  const btn = document.getElementById("provision-va-btn");
  if (!btn) return;
  btn.disabled = true;
  btn.textContent = "Provisioning\u2026";
  try {
    const res = await API.wallet.provisionVirtualAccount();
    toast(res.already_existed ? "Virtual account already provisioned." : "Virtual account created!", "success");
    renderVirtualAccount(res.virtual_account);
  } catch (err) {
    toast(err.message, "error");
    btn.disabled = false;
    btn.textContent = "Provision virtual account";
  }
}

function renderTransactions(txns) {
  const container = document.getElementById("wallet-transactions");
  if (!txns || txns.length === 0) {
    container.innerHTML = `<div class="empty"><div class="icon">\uD83D\uDCB0</div><p>No transactions yet.</p></div>`;
    return;
  }
  container.innerHTML = `<table class="table"><thead><tr><th>Date</th><th>Type</th><th>Amount</th><th>Fee</th><th>Status</th><th>Narration</th></tr></thead><tbody>${txns.map((t) => `
    <tr>
      <td>${fmtDateFull(t.created_at)}</td>
      <td>${statusBadge(t.type)}</td>
      <td class="${t.type === "credit" ? "text-lime" : "text-red"}">${t.type === "credit" ? "+" : "\u2212"}${fmtMoney(t.amount)}</td>
      <td>${fmtMoney(t.fee)}</td>
      <td>${statusBadge(t.status)}</td>
      <td class="text-xs text-mute">${esc(t.narration || "\u2014")}</td>
    </tr>`).join("")}</tbody></table>`;
}

async function loadTransactions(type) {
  const container = document.getElementById("wallet-transactions");
  try {
    const txns = await API.wallet.transactions(20, type || undefined);
    renderTransactions(txns);
  } catch (err) {
    container.innerHTML = `<div class="empty text-red">${esc(err.message)}</div>`;
  }
}

async function handleTopup() {
  const input = document.getElementById("topup-amount");
  const btn = document.getElementById("topup-btn");
  const amount = parseFloat(input.value);

  if (!amount || amount < 100) {
    toast("Minimum top-up is \u20A6100.", "error");
    return;
  }

  btn.disabled = true;
  btn.textContent = "Opening\u2026";

  try {
    const res = await API.wallet.topup(amount, window.location.origin + "/wallet");
    toast("Redirecting to Nomba Checkout\u2026", "info");
    window.open(res.authorization_url, "_blank");
    // After the user pays on the hosted Checkout page, they come
    // back here and can tap "I've paid" to verify the credit.
    // Stash the reference in sessionStorage so the button survives
    // a page refresh; clear it once the credit is confirmed.
    sessionStorage.setItem("pending_topup_ref", res.reference);
    sessionStorage.setItem("pending_topup_amount", String(res.amount));
    renderPendingTopupCard(res.reference, res.amount);
  } catch (err) {
    toast(err.message, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = "Top up";
  }
}

function renderPendingTopupCard(reference, amount) {
  // If a pending-topup card is already on the page, remove it so
  // we don't stack duplicates on repeated Top up clicks.
  const existing = document.getElementById("pending-topup-card");
  if (existing) existing.remove();
  // Find the wallet page's transactions container and prepend
  // the pending-topup card there.
  const txnContainer = document.getElementById("wallet-transactions");
  if (!txnContainer) return;
  const card = document.createElement("div");
  card.id = "pending-topup-card";
  card.className = "card mb-4 p-4 border-l-4 border-lime";
  card.innerHTML = `
    <div class="flex items-start justify-between gap-3">
      <div>
        <div class="text-sm text-gray-500">Pending top-up</div>
        <div class="text-lg font-bold">\u20A6${fmtMoney(amount)}</div>
        <div class="text-xs text-gray-400 font-mono">${reference}</div>
      </div>
      <div class="flex flex-col gap-2">
        <button id="pending-topup-verify" class="btn-lime text-sm py-2 px-3">
          \u2705 I've paid
        </button>
        <button id="pending-topup-dismiss" class="btn-secondary text-sm py-1 px-3">
          Dismiss
        </button>
      </div>
    </div>
    <div class="text-xs text-gray-500 mt-2">
      Paid on the provider's page? Tap <strong>I've paid</strong> to
      check if your wallet has been credited.
    </div>
  `;
  txnContainer.parentNode.insertBefore(card, txnContainer);
  document.getElementById("pending-topup-verify").onclick = () => verifyTopup(reference);
  document.getElementById("pending-topup-dismiss").onclick = () => {
    card.remove();
    sessionStorage.removeItem("pending_topup_ref");
    sessionStorage.removeItem("pending_topup_amount");
  };
}

async function verifyTopup(reference) {
  const btn = document.getElementById("pending-topup-verify");
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Verifying\u2026";
  }
  try {
    const res = await API.wallet.verifyTopup(reference);
    if (res.credited) {
      toast(res.message, "success");
      // Clear the pending card + storage; reload wallet to show new balance.
      sessionStorage.removeItem("pending_topup_ref");
      sessionStorage.removeItem("pending_topup_amount");
      const card = document.getElementById("pending-topup-card");
      if (card) card.remove();
      // Refresh the wallet page so balance + transactions update.
      if (currentRoute() === "/wallet") loadWallet();
    } else if (res.status === "already_credited") {
      toast(res.message, "success");
      sessionStorage.removeItem("pending_topup_ref");
      sessionStorage.removeItem("pending_topup_amount");
      const card = document.getElementById("pending-topup-card");
      if (card) card.remove();
      if (currentRoute() === "/wallet") loadWallet();
    } else if (res.status === "provider_pending") {
      toast("Still pending. Give it a few seconds and try again.", "info");
    } else if (res.status === "provider_failed") {
      toast(res.message, "error");
      sessionStorage.removeItem("pending_topup_ref");
      sessionStorage.removeItem("pending_topup_amount");
      const card = document.getElementById("pending-topup-card");
      if (card) card.remove();
    } else {
      toast(res.message, "error");
    }
  } catch (err) {
    toast(err.message || "Verification failed. Please try again.", "error");
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "\u2705 I've paid";
    }
  }
}

// ── Page: Telegram ────────────────────────────────────────────────────────
function renderTelegram() {
  return `
    <div class="page-content fade-up max-w-xl">
      <div class="page-header">
        <div class="page-eyebrow">TELEGRAM</div>
        <h1 class="page-title">Telegram link</h1>
      </div>

      <div id="tg-status" class="card p-6 mb-6">
        <p class="text-mute">Loading\u2026</p>
      </div>

      <div class="card p-6 mb-6">
        <h3 class="font-syne font-bold mb-2">How to link</h3>
        <ol class="text-sm text-mute space-y-2" style="list-style:decimal;padding-left:1.5rem;">
          <li>Generate a link code below.</li>
          <li>Open the Telegram bot and send <code class="text-lime">/link YOUR_CODE</code>.</li>
          <li>Your account will be linked automatically.</li>
        </ol>
        <div class="flex gap-3 mt-6">
          <button class="btn-lime" id="tg-gen-btn" onclick="generateLinkCode()">Generate link code</button>
          <button class="btn-danger" id="tg-unlink-btn" onclick="unlinkTelegram()" style="display:none">Unlink Telegram</button>
        </div>
      </div>

      <div id="tg-code-display" style="display:none" class="card p-6 lime-glow text-center">
        <div class="label mb-2">Your link code</div>
        <div id="tg-code-value" class="text-3xl font-bold font-mono text-lime"></div>
        <div class="text-xs text-mute mt-2">Expires in 15 minutes. Use it in the Telegram bot.</div>
      </div>
    </div>`;
}

async function loadTelegram() {
  const statusDiv = document.getElementById("tg-status");
  const unlinkBtn = document.getElementById("tg-unlink-btn");
  try {
    const me = await API.auth.me();
    if (me.is_telegram_linked) {
      statusDiv.innerHTML = `
        <div class="flex items-center gap-3">
          <span class="dot dot-on"></span>
          <div>
            <div class="font-bold">Telegram linked</div>
            <div class="text-xs text-mute">Your account is connected to Telegram.</div>
          </div>
        </div>`;
      unlinkBtn.style.display = "";
    } else {
      statusDiv.innerHTML = `
        <div class="flex items-center gap-3">
          <span class="dot dot-off"></span>
          <div>
            <div class="font-bold">Not linked</div>
            <div class="text-xs text-mute">Link your Telegram to receive bill notifications.</div>
          </div>
        </div>`;
      unlinkBtn.style.display = "none";
    }
  } catch (err) {
    statusDiv.innerHTML = `<p class="text-red">${esc(err.message)}</p>`;
  }
}

async function generateLinkCode() {
  const btn = document.getElementById("tg-gen-btn");
  btn.disabled = true;
  btn.textContent = "Generating\u2026";
  try {
    const res = await API.auth.linkCode();
    const display = document.getElementById("tg-code-display");
    const codeValue = document.getElementById("tg-code-value");
    display.style.display = "";
    codeValue.textContent = res.code;
    if (res.bot_link) {
      display.innerHTML += `<div class="mt-4"><a href="${esc(res.bot_link)}" target="_blank" class="btn-lime text-xs">Open in Telegram</a></div>`;
    }
  } catch (err) {
    toast(err.message, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = "Generate link code";
  }
}

async function unlinkTelegram() {
  showModal("Unlink Telegram?", "Your Telegram bot will stop sending you notifications.", [
    { label: "Cancel", cls: "btn-outline" },
    { label: "Unlink", cls: "btn-danger", fn: async () => {
      try {
        await API.auth.unlinkTelegram();
        toast("Telegram unlinked.", "success");
        loadTelegram();
        updateNav();
      } catch (err) { toast(err.message, "error"); }
    }},
  ]);
}

// ── Page: KYC ────────────────────────────────────────────────────────────
// (not in the nav but accessible via the dashboard if needed)
function renderKyc() {
  return `
    <div class="page-content fade-up max-w-md">
      <div class="page-header">
        <div class="page-eyebrow">KYC</div>
        <h1 class="page-title">BVN verification</h1>
      </div>
      <div id="kyc-status" class="card p-6 mb-6"></div>
      <div id="kyc-form" class="card p-6"></div>
    </div>`;
}

async function loadKyc() {
  const statusDiv = document.getElementById("kyc-status");
  const formDiv = document.getElementById("kyc-form");
  try {
    const kyc = await API.kyc.getBvn();
    statusDiv.innerHTML = `
      <div class="flex items-center gap-3">
        <span class="dot dot-on"></span>
        <div>
          <div class="font-bold">BVN on file</div>
          <div class="text-xs text-mute">Last 4 digits: ${esc(kyc.bvn_last4)} ${kyc.bvn_validated ? "(validated)" : "(pending validation)"}</div>
        </div>
      </div>`;
    formDiv.innerHTML = "";
  } catch (err) {
    if (err.message.includes("404") || err.message.includes("No KYC")) {
      statusDiv.innerHTML = `<p class="text-mute">No BVN on file yet.</p>`;
      formDiv.innerHTML = `
        <h3 class="font-syne font-bold mb-4">Submit your BVN</h3>
        <div>
          <label class="label">Bank Verification Number (11 digits)</label>
          <input class="input" id="kyc-bvn-input" maxlength="11" pattern="\\d{11}" placeholder="12345678901" />
        </div>
        <button class="btn-lime w-full mt-4" onclick="submitBvn()">Submit BVN</button>`;
    } else {
      statusDiv.innerHTML = `<p class="text-red">${esc(err.message)}</p>`;
    }
  }
}

async function submitBvn() {
  const input = document.getElementById("kyc-bvn-input");
  const bvn = input.value.trim();
  if (!/^\d{11}$/.test(bvn)) {
    toast("BVN must be exactly 11 digits.", "error");
    return;
  }
  try {
    await API.kyc.submitBvn(bvn);
    toast("BVN submitted successfully!", "success");
    loadKyc();
  } catch (err) {
    toast(err.message, "error");
  }
}

// ── Logout ────────────────────────────────────────────────────────────────
async function doLogout() {
  try {
    await API.auth.logout();
  } catch {
    // Best effort
  }
  API.clearTokens();
  updateNav();
  routeTo("/login");
}

// ── HTML Escaping ────────────────────────────────────────────────────────
function esc(str) {
  const d = document.createElement("div");
  d.textContent = str ?? "";
  return d.innerHTML;
}

// ── SPA Renderer ─────────────────────────────────────────────────────────
const PUBLIC_ROUTES = ["/login", "/signup"];

function render() {
  const route = getRoute();
  const app = document.getElementById("app");

  // Redirect logic
  if (!API.isLoggedIn() && !PUBLIC_ROUTES.includes(route)) {
    routeTo("/login");
    return;
  }
  if (API.isLoggedIn() && PUBLIC_ROUTES.includes(route)) {
    routeTo("/dashboard");
    return;
  }

  let html = "";
  let afterRender = null;

  switch (route) {
    case "/login":
      html = renderAuthPage("login");
      afterRender = () => {
        document.getElementById("auth-form")?.addEventListener("submit", (e) => handleAuthSubmit(e, "login"));
      };
      break;
    case "/signup":
      html = renderAuthPage("signup");
      afterRender = () => {
        document.getElementById("auth-form")?.addEventListener("submit", (e) => handleAuthSubmit(e, "signup"));
      };
      break;
    case "/dashboard":
      html = renderDashboard();
      afterRender = () => { loadDashboard(); };
      break;
    case "/bills":
      html = renderBills();
      afterRender = () => { loadBills(); };
      break;
    case "/upload":
      html = renderUpload();
      afterRender = () => { initUpload(); };
      break;
    case "/schedule":
      html = renderSchedule();
      afterRender = () => { loadSchedule(); };
      break;
    case "/wallet":
      html = renderWallet();
      afterRender = () => { loadWallet(); };
      break;
    case "/telegram":
      html = renderTelegram();
      afterRender = () => { loadTelegram(); };
      break;
    case "/kyc":
      html = renderKyc();
      afterRender = () => { loadKyc(); };
      break;
    default:
      routeTo(API.isLoggedIn() ? "/dashboard" : "/login");
      return;
  }

  app.innerHTML = html;
  highlightNav();
  updateNav();

  if (afterRender) afterRender();
}

// ── Boot ─────────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
  render();

  // Auto-refresh dashboard/wallet every 30s if logged in
  setInterval(() => {
    if (!API.isLoggedIn()) return;
    const route = getRoute();
    if (route === "/dashboard") loadDashboard();
    if (route === "/wallet") loadWallet();
  }, 30000);
});