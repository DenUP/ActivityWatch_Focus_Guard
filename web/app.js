"use strict";

const minuteButtons = [...document.querySelectorAll("[data-minutes]")];
const countdown = document.querySelector("#countdown");
const timerOrbit = document.querySelector("#timerOrbit");
const timerSubtext = document.querySelector("#timerSubtext");
const timerFootText = document.querySelector("#timerFootText");
const badge = document.querySelector("#statusBadge");
const badgeText = document.querySelector("#statusBadgeText");
const startButton = document.querySelector("#startButton");
const restoreButton = document.querySelector("#restoreButton");
const stateText = document.querySelector("#stateText");
const stateMessage = document.querySelector("#stateMessage");
const dialog = document.querySelector("#restoreDialog");
const toast = document.querySelector("#toast");

let chosenMinutes = 25;
let currentStatus = null;
let busy = false;
let toastTimeout = null;

function accessToken() {
  const params = new URLSearchParams(window.location.hash.slice(1));
  const supplied = params.get("token");
  if (supplied) {
    sessionStorage.setItem("focusGuardToken", supplied);
    history.replaceState(null, "", window.location.pathname);
  }
  return sessionStorage.getItem("focusGuardToken") || "";
}
const token = accessToken();

function clockTime() {
  document.querySelector("#clock").textContent = new Date().toLocaleTimeString("ru-RU", {
    hour: "2-digit", minute: "2-digit"
  });
}

function displayTime(seconds) {
  const rounded = Math.max(0, Math.ceil(seconds));
  return `${String(Math.floor(rounded / 60)).padStart(2, "0")}:${String(rounded % 60).padStart(2, "0")}`;
}

function setDuration(minutes) {
  if (currentStatus?.active || currentStatus?.blocked || busy) return;
  chosenMinutes = minutes;
  minuteButtons.forEach(button => {
    const selected = Number(button.dataset.minutes) === minutes;
    button.classList.toggle("selected", selected);
    button.setAttribute("aria-pressed", String(selected));
  });
  renderTimer();
}

function renderTimer() {
  const session = currentStatus;
  const active = !!session?.active;
  let seconds = chosenMinutes * 60;
  let total = seconds;
  if (active) {
    total = (session.minutes || chosenMinutes) * 60;
    seconds = session.ends_at ? Math.max(0, (Date.parse(session.ends_at) - Date.now()) / 1000)
      : session.remaining_seconds;
  }
  countdown.textContent = displayTime(seconds);
  timerOrbit.style.setProperty("--progress", `${active ? Math.max(0, Math.min(100, 100 * (1 - seconds / total))) : 0}%`);
  timerSubtext.textContent = active ? "до конца учебной сессии" : "минут концентрации";
}

function renderStatus(status) {
  currentStatus = status;
  const messages = {
    ready: ["Готов к старту", "Блокировка выключена. YouTube доступен.", "Небольшая сессия — хороший способ начать."],
    pending: ["Ожидаем Windows", "Подтверди запрос администратора Windows. Если отменил, подожди немного.", "Ещё один шаг — и можно сосредоточиться."],
    focus: ["Сессия активна", "Блокировка Windows установлена. Переключение окон не сбросит таймер.", "Выдели это время одной важной задаче."],
    warning: ["Проверить блокировку", "Таймер идёт, но блокировку hosts не удалось обнаружить. Проверь запуск от администратора.", "Проверь доступ к YouTube и состояние Windows."],
    stale: ["Нужна проверка", "Время вышло, а ограничения hosts ещё обнаружены. Используй аварийное восстановление.", "Доступ должен автоматически восстановиться."],
    error: ["Ошибка Windows", "Не удалось прочитать системный файл hosts. Проверь разрешения или восстанови доступ.", "Проверка состояния требует внимания."]
  };
  const [label, text, foot] = messages[status.state] || messages.error;
  badgeText.textContent = label;
  badge.className = `badge ${status.state === "focus" ? "badge-focus" :
    ["warning", "stale", "error"].includes(status.state) ? "badge-warning" : "badge-ready"}`;
  stateMessage.textContent = !token ? "Для управления открой эту страницу через значок Focus Guard в трее." : text;
  stateText.classList.toggle("warning", ["warning", "stale", "error"].includes(status.state));
  timerFootText.textContent = foot;
  document.querySelector("#activityWatchStatus").textContent = status.activitywatch_connected
    ? "ActivityWatch подключён." : "ActivityWatch сейчас не подключён.";

  const lockSelection = status.active || status.blocked || status.state === "pending" || busy;
  minuteButtons.forEach(button => { button.disabled = lockSelection; });
  startButton.disabled = lockSelection || !token || status.state === "error";
  startButton.querySelector("span").textContent = status.active ? "Сессия уже идёт" :
    status.state === "pending" ? "Ожидаем подтверждения…" : "Начать учебную сессию";
  restoreButton.hidden = !(status.active || status.blocked);
  restoreButton.disabled = busy || !token;
  renderTimer();
}

function showToast(text, isError = false) {
  if (toastTimeout) clearTimeout(toastTimeout);
  toast.textContent = text;
  toast.classList.toggle("error", isError);
  toast.classList.add("show");
  toastTimeout = setTimeout(() => toast.classList.remove("show"), 5200);
}

async function requestAction(path, payload) {
  if (!token || busy) return;
  busy = true;
  if (currentStatus) renderStatus(currentStatus);
  try {
    const response = await fetch(path, {
      method: "POST",
      cache: "no-store",
      headers: { "Content-Type": "application/json", "X-FocusGuard-Token": token },
      body: JSON.stringify(payload)
    });
    const result = await response.json();
    if (!response.ok || !result.ok) throw new Error(result.error || `Ошибка ${response.status}`);
    showToast(result.pending ? "Подтверди запрос администратора Windows." : "Действие выполнено.");
    await refreshStatus();
  } catch (error) {
    showToast(String(error.message || error), true);
  } finally {
    busy = false;
    if (currentStatus) renderStatus(currentStatus);
  }
}

async function refreshStatus() {
  try {
    const response = await fetch("/api/status", { cache: "no-store" });
    if (!response.ok) throw new Error("Focus Guard не отвечает");
    renderStatus(await response.json());
  } catch (_) {
    badgeText.textContent = "Нет соединения";
    badge.className = "badge badge-warning";
    stateMessage.textContent = "Локальное приложение недоступно. Запусти Focus Guard.exe.";
    startButton.disabled = true;
    restoreButton.disabled = true;
  }
}

minuteButtons.forEach(button => button.addEventListener("click", () => setDuration(Number(button.dataset.minutes))));
startButton.addEventListener("click", () => requestAction("/api/start", { minutes: chosenMinutes }));
restoreButton.addEventListener("click", () => dialog.showModal());
dialog.addEventListener("close", () => {
  if (dialog.returnValue === "confirm") requestAction("/api/restore", {});
});

setDuration(25);
clockTime();
refreshStatus();
setInterval(clockTime, 1000);
setInterval(renderTimer, 1000);
setInterval(refreshStatus, 1800);
