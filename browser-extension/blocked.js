"use strict";

async function displayCountdown() {
  const stored = await chrome.storage.local.get("studyDeadline");
  const left = Math.max(0, Math.ceil(((Number(stored.studyDeadline) || 0) - Date.now()) / 1000));
  const countdown = document.querySelector("#countdown");
  countdown.textContent = left
    ? `До конца сессии: ${Math.floor(left / 60)} мин ${left % 60} сек`
    : "Сессия закончилась. Можно закрыть эту вкладку.";
}

displayCountdown();
setInterval(displayCountdown, 1000);
