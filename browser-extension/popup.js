"use strict";

async function update() {
  const state = document.querySelector("#state");
  const detail = document.querySelector("#detail");
  state.textContent = "Проверка…";
  try {
    const result = await chrome.runtime.sendMessage({ type: "FOCUS_GUARD_SYNC" });
    if (!result || result.error) throw new Error(result?.error || "Нет ответа расширения");
    if (result.active) {
      state.textContent = "YouTube заблокирован";
      detail.textContent = "До " + new Date(result.endsAt).toLocaleTimeString("ru-RU", {
        hour: "2-digit", minute: "2-digit"
      }) + ". " + (result.appOnline ? "Программа подключена." : "Программа закрыта; блокировка сохраняется.");
    } else {
      state.textContent = "Блокировка выключена";
      detail.textContent = result.appOnline ? "Начни учебную сессию в Focus Guard."
        : "Запусти Focus Guard.exe на Windows.";
    }
  } catch (error) {
    state.textContent = "Ошибка синхронизации";
    detail.textContent = String(error);
  }
}

document.querySelector("#check").addEventListener("click", update);
update();
