/* ==========================================================================
   SATO — трекинг + реальная отправка формы (вставляется ВНУТРИ существующего
   IIFE, после рендера DOM, #apply-form уже существует).
   НЕ оборачивать в ещё один IIFE / DOMContentLoaded.
   ========================================================================== */

/* ----- 1. МАРКЕТИНГОВЫЙ ЗАХВАТ (first-touch + last-touch) ----------------- */
try {
  // Читаем UTM и рекламные клиентские идентификаторы из URL
  var __readMkt = function () {
    var p = new URLSearchParams(location.search);
    var keys = [
      'utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term',
      'gclid', 'yclid', 'fbclid'
    ];
    var out = {};
    keys.forEach(function (k) {
      var v = p.get(k);
      if (v) out[k] = v;
    });
    return out;
  };

  var __mktNow = __readMkt();
  var __hasUtm = Object.keys(__mktNow).length > 0;

  // Metrika ClientID — ключ сквозной аналитики (склейка визит↔сделка↔оплата).
  // getClientID асинхронный: ловим в window.__ymClientId, к моменту сабмита готов.
  window.__ymClientId = window.__ymClientId || '';
  var __capYmId = function () {
    try {
      if (window.ym && window.__ymId) {
        ym(window.__ymId, 'getClientID', function (id) { window.__ymClientId = id || ''; });
      }
    } catch (e) {}
  };
  __capYmId();
  setTimeout(__capYmId, 1500);

  // FIRST-TOUCH: сохраняем только если ещё ничего не записано
  try {
    if (!localStorage.getItem('sato_mkt')) {
      var firstTouch = {};
      Object.keys(__mktNow).forEach(function (k) { firstTouch[k] = __mktNow[k]; });
      firstTouch.referrer = document.referrer;
      firstTouch.landing = location.href;
      firstTouch.first_visit_ts = new Date().toISOString();
      localStorage.setItem('sato_mkt', JSON.stringify(firstTouch));
    }
  } catch (e) {}

  // LAST-TOUCH: обновляем при наличии utm-параметров в текущем URL
  try {
    if (__hasUtm) {
      localStorage.setItem('sato_mkt_last', JSON.stringify(__mktNow));
    }
  } catch (e) {}

  // getMkt(): объединяет first-touch + текущий page_url
  var getMkt = function () {
    var merged = {};
    try {
      var ft = localStorage.getItem('sato_mkt');
      if (ft) {
        var parsed = JSON.parse(ft);
        Object.keys(parsed).forEach(function (k) { merged[k] = parsed[k]; });
      }
    } catch (e) {}
    merged.page_url = location.href;
    return merged;
  };

  /* ----- 4. trackGoal — цели Яндекс.Метрики / Google Analytics ----------- */
  var trackGoal = function (goal) {
    try { if (window.ym && window.__ymId) ym(window.__ymId, 'reachGoal', goal); } catch (e) {}
    try { if (window.gtag) gtag('event', goal); } catch (e) {}
  };

  /* ----- 2. ОТПРАВКА ФОРМЫ (заменяет фейковый обработчик) ---------------- */
  var form = document.getElementById('apply-form');
  if (form) {
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      try {
        // Безопасное чтение значений полей
        var val = function (sel) {
          var el = form.querySelector(sel);
          return el && typeof el.value === 'string' ? el.value.trim() : '';
        };
        var name = val('[name="name"]');
        var phone = val('[name="phone"]');
        var email = val('[name="email"]');
        var city = val('[name="city"]');
        var format = val('[name="format"]');
        var channel = val('[name="channel"]');
        var message = val('[name="message"]');

        // Валидация обязательных полей
        if (!name || !phone || !email) {
          alert('Заполните имя, телефон и e-mail.');
          return;
        }

        // Проверка согласия на обработку данных
        var consentEl = form.querySelector('[name="consent"]');
        if (!consentEl || !consentEl.checked) {
          alert('Подтвердите согласие на обработку данных.');
          return;
        }

        var btn = form.querySelector('button[type="submit"]');
        var originalText = btn ? btn.innerHTML : '';

        // Сбор payload: поля формы + маркетинг + метаданные
        var payload = {
          name: name,
          phone: phone,
          email: email,
          city: city,
          format: format,
          channel: channel,
          message: message,
          consent: true,
          page_url: location.href,
          submit_ts: new Date().toISOString(),
          user_agent: navigator.userAgent,
          screen: screen.width + 'x' + screen.height
        };
        var mkt = getMkt();
        Object.keys(mkt).forEach(function (k) { payload[k] = mkt[k]; });
        payload.ym_client_id = window.__ymClientId || '';

        // Блокируем кнопку на время отправки
        if (btn) {
          btn.disabled = true;
          btn.innerHTML = 'Отправляем';
        }

        // Таймаут 12 секунд через AbortController
        var controller = new AbortController();
        var timeoutId = setTimeout(function () { controller.abort(); }, 12000);

        fetch('/api/lead', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
          signal: controller.signal
        }).then(function (res) {
          clearTimeout(timeoutId);
          if (res.ok) {
            // Успех: показываем сообщение, фиксируем кнопку, шлём цель
            var success = document.getElementById('form-success');
            if (success) success.style.display = 'block';
            if (btn) {
              btn.innerHTML = 'Отправлено';
              btn.disabled = true;
            }
            trackGoal('lead_submit');
          } else {
            throw new Error('Bad response: ' + res.status);
          }
        }).catch(function (err) {
          clearTimeout(timeoutId);
          // Ошибка/таймаут: возвращаем кнопку в исходное состояние
          if (btn) {
            btn.disabled = false;
            btn.innerHTML = originalText;
          }
          alert('Не удалось отправить. Попробуйте ещё раз или напишите нам.');
        });
      } catch (err) {
        // На любой непредвиденный сбой — не ломаем страницу
        try {
          var btn2 = form.querySelector('button[type="submit"]');
          if (btn2) { btn2.disabled = false; }
        } catch (e2) {}
        alert('Не удалось отправить. Попробуйте ещё раз или напишите нам.');
      }
    });
  }

  /* ----- 3. ТРЕКИНГ КЛИКОВ ПО CTA --------------------------------------- */
  try {
    // Собираем все интересующие элементы
    var ctaSelectors = 'a.nav-cta, a.btn, a.price, [data-format], #apply-form button[type="submit"]';
    var ctaNodes = document.querySelectorAll(ctaSelectors);

    var sendEvent = function (el) {
      try {
        var rawName = (el.textContent || '').trim().slice(0, 60);
        var evt = {
          type: 'click',
          name: rawName,
          target: (el.getAttribute('href') || el.id || ''),
          page_url: location.href,
          ts: new Date().toISOString()
        };
        // Добавляем текущие utm-параметры
        var u = __readMkt();
        Object.keys(u).forEach(function (k) { evt[k] = u[k]; });

        var json = JSON.stringify(evt);
        // Неблокирующая отправка — не мешает переходу по ссылке
        if (navigator.sendBeacon) {
          var blob = new Blob([json], { type: 'application/json' });
          navigator.sendBeacon('/api/event', blob);
        } else {
          fetch('/api/event', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: json,
            keepalive: true
          }).catch(function () {});
        }
      } catch (e) {}
    };

    if (ctaNodes && ctaNodes.length) {
      Array.prototype.forEach.call(ctaNodes, function (el) {
        el.addEventListener('click', function () {
          // Никогда не вызываем preventDefault — навигация по anchor должна работать
          sendEvent(el);
        });
      });
    }
  } catch (e) {}

} catch (e) {
  // Глобальный предохранитель: сбой трекинга не должен ломать страницу/форму
}
