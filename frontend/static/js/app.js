// Global App JS
document.addEventListener("DOMContentLoaded", () => {
  initClock();
  initModals();
});

// --------------------------------------------------------------------------
// Real-time Clock (IST: UTC+05:30)
// --------------------------------------------------------------------------
function initClock() {
  const clockEl = document.getElementById("live-clock");
  if (!clockEl) return;

  function update() {
    const now = new Date();
    // UTC time + 5.5 hours for IST
    const utcTime = now.getTime() + now.getTimezoneOffset() * 60000;
    const istTime = new Date(utcTime + 5.5 * 3600000);
    
    const timeStr = istTime.toLocaleTimeString("en-GB", { hour12: false });
    const dateStr = istTime.toISOString().split("T")[0];
    clockEl.textContent = `${dateStr} ${timeStr} IST`;
  }
  update();
  setInterval(update, 1000);
}

// --------------------------------------------------------------------------
// Toast Notifications
// --------------------------------------------------------------------------
function showToast(message, type = "info") {
  let container = document.getElementById("toast-container");
  if (!container) {
    container = document.createElement("div");
    container.id = "toast-container";
    container.className = "toast-container";
    document.body.appendChild(container);
  }

  const toast = document.createElement("div");
  toast.className = `toast ${type}`;
  
  const icon = type === "success" ? "✓" : type === "error" ? "⚠" : "ℹ";
  toast.innerHTML = `<span style="font-weight: bold;">${icon}</span> <span>${message}</span>`;

  container.appendChild(toast);
  setTimeout(() => {
    toast.style.opacity = "0";
    toast.style.transform = "translateX(50px)";
    toast.style.transition = "all 0.3s ease";
    setTimeout(() => toast.remove(), 300);
  }, 4000);
}

// --------------------------------------------------------------------------
// Modals
// --------------------------------------------------------------------------
function openModal(id) {
  const modal = document.getElementById(id);
  if (modal) modal.classList.add("active");
}

function closeModal(id) {
  const modal = document.getElementById(id);
  if (modal) modal.classList.remove("active");
}

function initModals() {
  document.querySelectorAll(".modal-overlay").forEach((modal) => {
    modal.addEventListener("click", (e) => {
      if (e.target === modal) modal.classList.remove("active");
    });
  });
}

// --------------------------------------------------------------------------
// Timestamp helpers
// --------------------------------------------------------------------------
function formatEpochMs(ms) {
  if (!ms) return "-";
  // convert to IST string
  const dt = new Date(ms);
  const utc = dt.getTime() + dt.getTimezoneOffset() * 60000;
  const ist = new Date(utc + 5.5 * 3600000);
  return ist.toISOString().replace("T", " ").substring(0, 19);
}

function localInputToEpochMs(datetimeLocalStr) {
  if (!datetimeLocalStr) return null;
  // datetime-local input is in local browser time. Parse as Date and return epoch ms
  const dt = new Date(datetimeLocalStr);
  return dt.getTime();
}

// --------------------------------------------------------------------------
// Quick Punch Handlers (consumed on dashboard & attendance page)
// --------------------------------------------------------------------------
async function handlePunchIn(e) {
  e.preventDefault();
  const form = e.target;
  const emp_code = form.emp_code.value.trim().toUpperCase();
  const status = form.status.value;
  const customTime = form.custom_time.value;

  const payload = { emp_code, status };
  if (customTime) {
    payload.punched_at = localInputToEpochMs(customTime);
  }

  try {
    const res = await fetch("/api/attendance/punch-in", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (res.ok) {
      showToast(`Punched IN: ${data.emp_code} on ${data.date} (Late: ${data.late_minutes}m)`, "success");
      closeModal("modal-punch-in");
      setTimeout(() => window.location.reload(), 1000);
    } else {
      showToast(data.detail || "Punch-in failed", "error");
    }
  } catch (err) {
    showToast("Error connecting to server", "error");
  }
}

async function handlePunchOut(e) {
  e.preventDefault();
  const form = e.target;
  const emp_code = form.emp_code.value.trim().toUpperCase();
  const customTime = form.custom_time.value;

  const payload = { emp_code };
  if (customTime) {
    payload.punched_at = localInputToEpochMs(customTime);
  }

  try {
    const res = await fetch("/api/attendance/punch-out", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (res.ok) {
      showToast(`Punched OUT: ${data.emp_code} (Work: ${data.work_hours || 0}h, OT: ${data.overtime_minutes || 0}m)`, "success");
      closeModal("modal-punch-out");
      setTimeout(() => window.location.reload(), 1000);
    } else {
      showToast(data.detail || "Punch-out failed", "error");
    }
  } catch (err) {
    showToast("Error connecting to server", "error");
  }
}

async function handleCreateEmployee(e) {
  e.preventDefault();
  const form = e.target;
  const payload = {
    emp_code: form.emp_code.value.trim().toUpperCase(),
    name: form.name.value.trim(),
    email: form.email.value.trim(),
    department: form.department.value.trim(),
    shift_start: form.shift_start.value,
    shift_end: form.shift_end.value,
    joined_on: form.joined_on.value,
  };

  try {
    const res = await fetch("/api/employees", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (res.ok) {
      showToast(`Employee ${data.emp_code} created successfully!`, "success");
      closeModal("modal-add-employee");
      setTimeout(() => window.location.reload(), 1000);
    } else {
      showToast(data.detail || "Failed to create employee", "error");
    }
  } catch (err) {
    showToast("Server error", "error");
  }
}
