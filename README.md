# USDCASH transfers monitor

Мониторинг поступлений USDCASH на аккаунт `4store.pcash` (сеть Vaulta / ex-EOS).

Архитектура:
- **monitor.py** — собирает данные и пишет `public/index.html`.
- **GitHub Actions** — запускает `monitor.py` и коммитит результат обратно
  в репозиторий. Запускается **не по встроенному cron**, а только по
  команде (`workflow_dispatch`) — извне, через GitHub REST API.
- **cron-job.org** — бесплатный внешний крон-сервис, который раз в 15 минут
  дёргает GitHub API и запускает workflow.
- **Vercel** — раздаёт папку `public/` как статический сайт, автоматически
  передеплоивается на каждый новый коммит от Actions.

## Настройка (по порядку)

### 1. Залить репозиторий на GitHub
```bash
cd pcash_monitor_repo
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/ТВОЙ_НИК/pcash-monitor.git
git push -u origin main
```

### 2. Разрешить Actions делать коммиты
Settings → Actions → General → Workflow permissions →
**Read and write permissions** → Save.

### 3. Создать Personal Access Token (понадобится для cron-job.org)
GitHub → фото профиля → Settings → Developer settings →
Personal access tokens → Tokens (classic) → Generate new token (classic).
- Scope: достаточно **repo** (или **public_repo**, если репозиторий публичный).
- Срок действия — на своё усмотрение (можно "No expiration").
- Скопировать токен сразу — он показывается один раз.

### 4. Настроить cron-job.org
Подробная инструкция — см. ниже в разделе "cron-job.org setup".

### 5. Подключить Vercel
vercel.com → Add New → Project → импортировать репозиторий.
Vercel сам подхватит `vercel.json` и будет раздавать `public/` как статику.

## cron-job.org setup

1. Зарегистрироваться / войти на https://console.cron-job.org/
2. **Create cronjob**
3. **Title:** что угодно, например `pcash monitor trigger`
4. **URL:**
   ```
   https://api.github.com/repos/ТВОЙ_НИК/pcash-monitor/actions/workflows/update-report.yml/dispatches
   ```
   (замени `ТВОЙ_НИК/pcash-monitor` на свой логин и имя репозитория)
5. **Request method:** `POST`
6. **Headers** (добавить две):
   - `Authorization: Bearer ТВОЙ_PERSONAL_ACCESS_TOKEN`
   - `Accept: application/vnd.github+json`
7. **Request body** (включить "Send custom body", тип JSON):
   ```json
   {"ref":"main"}
   ```
8. **Schedule:** Every 15 minutes (или User-defined → */15 * * * *)
9. Save → Enable.

GitHub отвечает на такой запрос пустым телом и кодом **204 No Content** —
это нормально, значит запуск принят. Проверить, что всё сработало, можно
во вкладке **Actions** репозитория на GitHub — там появится новый запуск
workflow.

## Ручной запуск (без cron-job.org)

В вкладке **Actions** репозитория на GitHub можно нажать **Run workflow**,
чтобы обновить отчёт немедленно.
