const API_BASE = window.APP_CONFIG.apiBase;
const POLL_INTERVAL_MS = 1000;

const grid = document.getElementById("seat-grid");
const userInput = document.getElementById("user-id");
const statusMessage = document.getElementById("status-message");
const statusText = document.getElementById("status-text");
const statusClose = document.getElementById("status-close");
const logoutBtn = document.getElementById("logout-btn");

function getUserId() {
  let userId = localStorage.getItem("userId");
  while (!userId) {
    userId = window.prompt("사용자 ID를 입력하세요", "user-1000");
    if (userId) {
      userId = userId.trim();
    }
  }
  localStorage.setItem("userId", userId);
  return userId;
}

userInput.value = getUserId();
userInput.addEventListener("change", () => {
  const value = userInput.value.trim() || getUserId();
  localStorage.setItem("userId", value);
  userInput.value = value;
});

logoutBtn.addEventListener("click", () => {
  localStorage.removeItem("userId");
  location.reload();
});

function showMessage(text, type = "error") {
  statusText.textContent = text;
  statusMessage.classList.toggle("visible", Boolean(text));
  statusMessage.classList.toggle("success", type === "success");
}

statusClose.addEventListener("click", () => {
  statusText.textContent = "";
  statusMessage.classList.remove("visible");
});

function seatClassName(seat, userId) {
  if (seat.status === "AVAILABLE") return "available";
  if (seat.status === "LOCKED") return "locked";
  if (seat.status === "BOOKED") {
    return seat.locked_by === userId ? "mine" : "booked";
  }
  return "booked";
}

const ROW_ORDER = ["A", "B", "C", "D", "E", "F"];

function groupSeatsByRow(seats) {
  const rows = new Map();
  for (const seat of seats) {
    const match = seat.seat_id.match(/^([A-Za-z]+)(\d+)$/);
    if (!match) continue;
    const [, row, numStr] = match;
    if (!rows.has(row)) rows.set(row, []);
    rows.get(row).push({ ...seat, _num: parseInt(numStr, 10) });
  }
  for (const seatList of rows.values()) {
    seatList.sort((a, b) => a._num - b._num);
  }
  return rows;
}

function splitRowSections(seatList) {
  return {
    left: seatList.filter((s) => s._num < 10),
    center: seatList.filter((s) => s._num >= 10 && s._num <= 21),
    right: seatList.filter((s) => s._num >= 22),
  };
}

function makeSeatButton(seat, userId) {
  const button = document.createElement("button");
  const cls = seatClassName(seat, userId);
  button.className = `seat ${cls}`;
  button.textContent = seat.seat_id;
  button.disabled = cls === "locked" || cls === "booked";
  button.addEventListener("click", () => handleSeatClick(seat, cls));
  return button;
}

function makeRowLabel(row) {
  const label = document.createElement("span");
  label.className = "row-label";
  label.textContent = row;
  return label;
}

function renderSeats(seats) {
  const userId = userInput.value.trim() || getUserId();
  const rows = groupSeatsByRow(seats);
  grid.innerHTML = "";

  for (const row of ROW_ORDER) {
    const seatList = rows.get(row);
    if (!seatList) continue;

    const rowEl = document.createElement("div");
    rowEl.className = "theater-row";
    rowEl.appendChild(makeRowLabel(row));

    const { left, center, right } = splitRowSections(seatList);
    [left, center, right].forEach((section, idx) => {
      const sectionEl = document.createElement("div");
      sectionEl.className = "seat-section";
      section.forEach((seat) => sectionEl.appendChild(makeSeatButton(seat, userId)));
      rowEl.appendChild(sectionEl);
      if (idx < 2) {
        const aisle = document.createElement("div");
        aisle.className = "aisle";
        rowEl.appendChild(aisle);
      }
    });

    rowEl.appendChild(makeRowLabel(row));
    grid.appendChild(rowEl);
  }
}

async function fetchSeats() {
  try {
    const res = await fetch(`${API_BASE}/seats`);
    if (!res.ok) throw new Error(`GET /seats failed: ${res.status}`);
    const seats = await res.json();
    renderSeats(seats);
  } catch (err) {
    showMessage("좌석 정보를 불러오지 못했습니다");
  }
}

async function handleSeatClick(seat, cls) {
  const userId = userInput.value.trim() || getUserId();

  if (cls === "available") {
    const confirmed = window.confirm(`${seat.seat_id} 좌석을 예약할까요? (결제가 진행됩니다)`);
    if (!confirmed) return;
    try {
      const res = await fetch(`${API_BASE}/reservations`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ seat_id: seat.seat_id, user_id: userId }),
      });
      const body = await res.json();
      if (!res.ok) {
        showMessage(body.detail || `예약 실패 (${res.status})`);
      } else {
        showMessage(`${seat.seat_id} 예약 완료`, "success");
      }
    } catch (err) {
      showMessage("예약 요청 중 오류가 발생했습니다");
    }
    await fetchSeats();
    return;
  }

  if (cls === "mine") {
    const confirmed = window.confirm(`${seat.seat_id} 예약을 취소할까요?`);
    if (!confirmed) return;
    try {
      const res = await fetch(`${API_BASE}/reservations/${seat.seat_id}/cancel`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_id: userId }),
      });
      const body = await res.json();
      if (!res.ok) {
        showMessage(body.detail || `취소 실패 (${res.status})`);
      } else {
        showMessage(`${seat.seat_id} 취소 완료`, "success");
      }
    } catch (err) {
      showMessage("취소 요청 중 오류가 발생했습니다");
    }
    await fetchSeats();
  }
}

fetchSeats();
setInterval(fetchSeats, POLL_INTERVAL_MS);
