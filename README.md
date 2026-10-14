# Карта оповещений БПЛА

Читает публичный канал t.me/BelgorodDRONE (без Telegram-ключей), ставит красные круги на карте,
убирает их после «Отбой» или через 40 минут.

## Запуск на своём компьютере
1. Установите Python (python.org).
2. В папке проекта: `pip install -r requirements.txt`, затем `python app.py`
3. Откройте http://localhost:8000

## Выложить в интернет (Render.com, бесплатно)
1. Загрузите папку на GitHub (New repository → Upload files).
2. render.com → New → Web Service → выберите репозиторий.
3. Build Command: `pip install -r requirements.txt`
   Start Command: `gunicorn app:app --timeout 120`
4. После деплоя Render выдаст ссылку на сайт.
(На бесплатном тарифе сайт «засыпает» без посетителей, первая загрузка ~30–60 сек.)

## Настройки (переменные окружения)
CHANNEL (имя канала), TTL_MIN (минуты жизни метки), REGION и VIEWBOX (область поиска мест).
Неверно поставленное место можно задать вручную в places.json: {"малинов": [широта, долгота]}
