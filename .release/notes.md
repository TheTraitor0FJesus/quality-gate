# Quality Gate 4.3.0

Добавлено узкое разрешение для точного Dependabot auto-merge workflow: только
нечерновые PR бота в `main`, без обновлений с security alerts и только для
semver-patch. Gate отклоняет изменённый шаблон, лишние права и выполнение кода
из PR. Потребителям нужно отдельно обновить immutable pin и предоставить secret
`DEPENDABOT_ALERTS_TOKEN` с доступом на чтение Dependabot alerts и pull requests.
