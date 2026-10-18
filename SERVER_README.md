# Windows Server

Этот каталог содержит исходники. Работающий сервер хранит Python runtime, локальные модели и личную базу отдельно в `C:\TelegramAssistant`. Эти файлы не входят в GitHub-репозиторий.

Для обновления существующей установки используйте [deploy/four_features.ps1](deploy/four_features.ps1) и инструкцию в [README.md](README.md). Скрипт рассчитан на установленную задачу `TelegramPersonalAssistant` и локальный HTTP-прокси Happ на `127.0.0.1:10809`.

Проверка состояния на сервере:

```powershell
Get-ScheduledTask -TaskName TelegramPersonalAssistant | Select-Object State
Get-ScheduledTaskInfo -TaskName TelegramPersonalAssistant | Format-List LastRunTime,LastTaskResult
Get-Content 'C:\TelegramAssistant\data\bot-error.log' -Tail 20
```

Базу `data/bot.sqlite3` и защищённый токен храните только на сервере. При резервном копировании сначала остановите задачу, затем копируйте базу в закрытое хранилище. Публикация этих файлов раскроет переписки и доступ к боту.
