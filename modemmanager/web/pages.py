"""HTML-страницы: статус, история, настройки SIM.

Простые страницы без CSS-фреймворков. Данные обновляются самой страницей через
периодические запросы к JSON-API -- пользователю не нужно жать F5 (см.
web-interface spec, «Данные страницы обновляются»).
"""

from __future__ import annotations

from html import escape


def _html_head(title: str) -> str:
    return (
        "<!DOCTYPE html>\n"
        "<html lang=\"ru\">\n"
        "<head>\n"
        "<meta charset=\"utf-8\">\n"
        f"<title>{escape(title)}</title>\n"
        "<style>body{font-family:system-ui,sans-serif;max-width:960px;"
        "margin:1em auto;padding:0 1em}"
        "table{border-collapse:collapse;width:100%}"
        "th,td{border:1px solid #ccc;padding:.35em .5em;text-align:left}"
        "th{background:#f2f2f2}"
        ".fault{color:#a00;font-weight:bold}"
        ".ok{color:#080}"
        ".dim{color:#666}"
        "form label{display:block;margin:.4em 0}"
        "</style>\n"
        "</head>\n<body>\n"
    )


def _html_foot() -> str:
    return "</body>\n</html>\n"


def _nav(active: str = "") -> str:
    """Ссылки на основные страницы."""
    items = [("/", "Статус"), ("/history", "История"), ("/sims", "SIM-карты")]
    parts = []
    for path, label in items:
        marker = " (текущая)" if active == path else ""
        parts.append(f'<a href="{escape(path)}">{escape(label)}</a>{marker}')
    return "<p>" + " · ".join(parts) + "</p>\n"


def status_page() -> str:
    """Страница статуса модемов с автообновлением через JS.

    JS запрашивает ``/api/state`` каждые несколько секунд и перерисовывает
    таблицу. Никаких зависимых от CDN библиотек -- честный vanilla JS.
    """
    script = """
<script>
async function refresh(){
  try{
    const resp = await fetch('/api/state', {headers:{Accept:'application/json'}});
    if(!resp.ok) return;
    const data = await resp.json();
    render(data);
  }catch(e){/* автообновление тихое */}
}
function td(text, cls){
  const cell = document.createElement('td');
  cell.textContent = text == null ? '—' : String(text);
  if(cls) cell.className = cls;
  return cell;
}
function render(data){
  const tbody = document.getElementById('modems-body');
  if(!tbody) return;
  tbody.innerHTML = '';
  for(const m of data.modems){
    const row = document.createElement('tr');
    row.appendChild(td(m.status, m.status === 'fault' ? 'fault' : m.status === 'online' ? 'ok' : 'dim'));
    row.appendChild(td(m.usb_path));
    row.appendChild(td(m.sim_label || m.imsi || '—'));
    row.appendChild(td(m.imsi || '—'));
    row.appendChild(td(m.signal_dbm != null ? m.signal_dbm + ' дБм' : 'неизвестно'));
    row.appendChild(td((m.operator_name || m.operator_plmn || '—') + (m.roaming ? ' (роуминг)' : '')));
    row.appendChild(td((m.registration_voice || 'неизвестно') + ' / ' + (m.registration_data || 'неизвестно')));
    row.appendChild(td(m.storage_total != null ? m.storage_used + '/' + m.storage_total : '—'));
    row.appendChild(td(m.fault_reason || ''));
    tbody.appendChild(row);
  }
}
window.addEventListener('load', () => { refresh(); setInterval(refresh, 3000); });
</script>
"""
    body = (
        _html_head("Статус модемов")
        + _nav(active="/")
        + "<h1>Статус модемов</h1>\n"
        "<table>\n"
        "<thead><tr>"
        "<th>Состояние</th><th>USB</th><th>SIM</th><th>IMSI</th>"
        "<th>Сигнал</th><th>Оператор</th><th>Регистрация (голос/данные)</th>"
        "<th>Память SMS</th><th>Причина неисправности</th>"
        "</tr></thead>\n"
        "<tbody id=\"modems-body\"><tr><td colspan=\"9\">Загрузка...</td></tr></tbody>\n"
        "</table>\n"
        "<p class=\"dim\">Данные обновляются автоматически.</p>\n"
        + script
        + _html_foot()
    )
    return body


def history_page() -> str:
    """Страница истории с автозагрузкой и пометкой неполных сообщений."""
    script = """
<script>
async function loadHistory(){
  const imsi = document.getElementById('imsi').value.trim();
  const params = new URLSearchParams({limit:'200'});
  if(imsi) params.set('imsi', imsi);
  const resp = await fetch('/api/history?' + params.toString());
  if(!resp.ok) return;
  const items = await resp.json();
  const tbody = document.getElementById('history-body');
  tbody.innerHTML = '';
  for(const rec of items){
    const row = document.createElement('tr');
    const time = document.createElement('td');
    time.textContent = rec.at || '—';
    row.appendChild(time);
    const type = document.createElement('td');
    type.textContent = rec.type;
    row.appendChild(type);
    const sim = document.createElement('td');
    sim.textContent = rec.sim_label || rec.imsi || '—';
    row.appendChild(sim);
    const info = document.createElement('td');
    if(rec.type === 'sms'){
      info.textContent = (rec.from || '—') + ': ' + (rec.text || '');
      if(rec.incomplete){
        const flag = document.createElement('span');
        flag.textContent = ' [не полностью]';
        flag.style.color = '#a00';
        info.appendChild(flag);
      }
    } else if(rec.type === 'call'){
      info.textContent = (rec.number || 'скрыт') + (rec.outcome ? ' (' + rec.outcome + ')' : '');
    } else {
      info.textContent = JSON.stringify(rec);
    }
    row.appendChild(info);
    tbody.appendChild(row);
  }
}
window.addEventListener('load', () => { loadHistory(); });
document.addEventListener('submit', e => { e.preventDefault(); loadHistory(); });
</script>
"""
    body = (
        _html_head("История")
        + _nav(active="/history")
        + "<h1>История сообщений и вызовов</h1>\n"
        "<form>\n"
        "<label>IMSI (пусто = вся история): <input id=\"imsi\" name=\"imsi\"></label>\n"
        "<button type=\"submit\">Показать</button>\n"
        "</form>\n"
        "<table>\n"
        "<thead><tr>"
        "<th>Время</th><th>Тип</th><th>SIM</th><th>Подробности</th>"
        "</tr></thead>\n"
        "<tbody id=\"history-body\"><tr><td colspan=\"4\">Загрузка...</td></tr></tbody>\n"
        "</table>\n"
        + script
        + _html_foot()
    )
    return body


def sims_index_page(sim_imsis: list[str]) -> str:
    """Список SIM-карт из настроек со ссылками на страницы редактирования."""
    rows = "".join(
        f'<li><a href="/sims/{escape(imsi)}">{escape(imsi)}</a></li>'
        for imsi in sim_imsis
    )
    return (
        _html_head("SIM-карты")
        + _nav(active="/sims")
        + "<h1>SIM-карты в настройках</h1>\n"
        + (f"<ul>{rows}</ul>\n" if rows else "<p>Настроенных карт ещё нет.</p>\n")
        + "<p><a href=\"/sims/new\">Добавить SIM</a></p>\n"
        + _html_foot()
    )


def sim_settings_page(
    imsi: str,
    label: str,
    msisdn: str,
    plmn: str,
    chat_id: str,
    pin_set: bool,
    pin_attempts: int | None,
) -> str:
    """Страница настроек одной SIM. Значение PIN на странице отсутствует."""
    pin_display = "задан" if pin_set else "не задан"
    attempts = "неизвестно" if pin_attempts is None else str(pin_attempts)
    body = (
        _html_head(f"SIM {imsi}")
        + _nav(active="/sims")
        + f"<h1>SIM {escape(imsi)}</h1>\n"
        + f"<p>PIN-код: <b>{pin_display}</b>. Осталось попыток: {escape(attempts)}.</p>\n"
        "<form method=\"post\" action=\"/api/sims/"
        + escape(imsi)
        + "\" enctype=\"application/x-www-form-urlencoded\">\n"
        + f'<label>Имя: <input name="label" value="{escape(label)}"></label>\n'
        + f'<label>Номер телефона: <input name="msisdn" value="{escape(msisdn)}"></label>\n'
        + f'<label>Оператор (PLMN): <input name="plmn" value="{escape(plmn)}"></label>\n'
        + f'<label>Адресат уведомлений: <input name="chat_id" value="{escape(chat_id)}"></label>\n'
        "<label>PIN: <input name=\"pin\" type=\"password\" placeholder=\"оставьте пустым, чтобы не менять\"></label>\n"
        "<label><input type=\"checkbox\" name=\"pin_clear\" value=\"1\"> Удалить PIN</label>\n"
        "<button type=\"submit\">Сохранить</button>\n"
        "</form>\n"
        "<p class=\"dim\">Введённый PIN нигде не отображается и не возвращается назад.</p>\n"
        + _html_foot()
    )
    return body
