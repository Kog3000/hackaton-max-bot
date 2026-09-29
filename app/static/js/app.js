// Связь с MAX: системная кнопка «Назад», отклик при нажатии и шеринг карточки.
(function () {
  const wa = window.WebApp;

  // Кнопка «Назад» в шапке MAX ведёт по адресу из data-back.
  const back = document.querySelector('[data-back]');
  if (wa && wa.BackButton && back) {
    const go = () => { location.href = back.dataset.back; };
    wa.BackButton.show();
    wa.BackButton.onClick(go);
    window.addEventListener('pagehide', () => { wa.BackButton.offClick(go); wa.BackButton.hide(); });
  }

  // Короткая вибрация на нажатие кнопок и карточек.
  const quiet = (fn) => { try { const r = fn(); if (r && r.catch) r.catch(() => {}); } catch (e) {} };
  document.addEventListener('click', (e) => {
    if (e.target.closest('.btn, .card, .chip, .tabbar a')) quiet(() => wa && wa.HapticFeedback && wa.HapticFeedback.impactOccurred('light'));
  });

  // «Позвать компанию»: отправляем ссылку-диплинк в чат MAX или копируем её.
  const share = document.querySelector('[data-share-link]');
  if (share) {
    share.addEventListener('click', async () => {
      const text = share.dataset.shareText || '';
      const link = share.dataset.shareLink || '';
      if (wa && wa.shareMaxContent) {
        try { await wa.shareMaxContent({ text, link }); return; } catch (e) {}
      }
      try { await navigator.clipboard.writeText(text + ' ' + link); share.textContent = 'Ссылка скопирована'; } catch (e) {}
    });
  }

  // В группе фильтров кнопка «Все» и конкретные значения исключают друг друга.
  document.querySelectorAll('[data-group]').forEach((group) => {
    const all = group.querySelector('[data-all] input');
    const others = [...group.querySelectorAll('label:not([data-all]) input')];
    const sync = () => {
      const anyPicked = others.some((input) => input.checked);
      if (all) all.checked = !anyPicked;
      group.querySelectorAll('label').forEach((label) => {
        label.classList.toggle('chip--on', label.querySelector('input').checked);
      });
    };
    if (all) all.addEventListener('change', () => { others.forEach((input) => { input.checked = false; }); sync(); });
    others.forEach((input) => input.addEventListener('change', sync));
    sync();
  });

  // Ширина полосы записи приходит в data-атрибутах.
  document.querySelectorAll('.strip__open').forEach((bar) => {
    bar.style.left = bar.dataset.left + '%';
    bar.style.width = bar.dataset.width + '%';
  });

  // Тост исчезает сам и не остаётся в адресе.
  const toast = document.querySelector('.toast');
  if (toast) {
    setTimeout(() => toast.remove(), 3200);
    const url = new URL(location.href);
    if (url.searchParams.has('msg')) { url.searchParams.delete('msg'); history.replaceState({}, '', url); }
  }
})();
