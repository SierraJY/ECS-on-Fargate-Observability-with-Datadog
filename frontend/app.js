const API_BASE = window.APP_CONFIG.apiBase;
const POLL_INTERVAL_MS = 5000;

const grid = document.getElementById("seat-grid");
const userInput = document.getElementById("user-id");
const statusMessage = document.getElementById("status-message");
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

function showMessage(text) {
  statusMessage.textContent = text;
  if (text) {
    setTimeout(() => {
      if (statusMessage.textContent === text) {
        statusMessage.textContent = "";
      }
    }, 4000);
  }
}

function seatClassName(seat, userId) {
  if (seat.status === "AVAILABLE") return "available";
  if (seat.status === "LOCKED") return "locked";
  if (seat.status === "BOOKED") {
    return seat.locked_by === userId ? "mine" : "booked";
  }
  return "booked";
}

function sortSeatsNaturally(seats) {
  return [...seats].sort((a, b) => {
    const numA = parseInt(a.seat_id.replace(/\D/g, ""), 10);
    const numB = parseInt(b.seat_id.replace(/\D/g, ""), 10);
    return numA - numB;
  });
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

function renderSeats(seats) {
  const userId = userInput.value.trim() || getUserId();
  const sorted = sortSeatsNaturally(seats);
  grid.innerHTML = "";

  for (let i = 0; i < sorted.length; i += 4) {
    const row = document.createElement("div");
    row.className = "seat-row";
    const chunk = sorted.slice(i, i + 4);

    chunk.slice(0, 2).forEach((seat) => row.appendChild(makeSeatButton(seat, userId)));
    const aisle = document.createElement("div");
    aisle.className = "aisle";
    row.appendChild(aisle);
    chunk.slice(2, 4).forEach((seat) => row.appendChild(makeSeatButton(seat, userId)));

    grid.appendChild(row);
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
        showMessage(`${seat.seat_id} 예약 완료`);
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
        showMessage(`${seat.seat_id} 취소 완료`);
      }
    } catch (err) {
      showMessage("취소 요청 중 오류가 발생했습니다");
    }
    await fetchSeats();
  }
}

fetchSeats();
setInterval(fetchSeats, POLL_INTERVAL_MS);
