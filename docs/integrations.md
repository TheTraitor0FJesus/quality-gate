# Интеграции и автоматические обновления

Документ описывает, что делают CodeRabbit, Dependabot и release rollout в публичном
репозитории Quality Gate и в зарегистрированных приватных репозиториях-потребителях.

## CodeRabbit: комментарии к pull request

CodeRabbit автоматически просматривает pull request, направленные в `main`, и оставляет краткие
комментарии на русском языке. Он пропускает черновики. Настройки отключают одобрение и запрос
изменений, статусы и слияние. Замечания CodeRabbit не влияют на обязательную проверку Quality Gate.

Генерация сводки отключена через `reviews.high_level_summary: false`. Параметр
`reviews.high_level_summary_in_walkthrough: true` оставляет сводку вне описания PR даже при наличии
маркера `@coderabbitai summary`. Комментарии ревью остаются включены. См.
[настройки сводки CodeRabbit](https://docs.coderabbit.ai/pr-reviews/summaries).

При установке CodeRabbit выберите только публичный репозиторий Quality Gate. Не устанавливайте
приложение в приватные репозитории-потребители.

## Dependabot: предложения обновить зависимости

Dependabot каждую неделю проверяет закреплённые GitHub Actions и Python-зависимости в корне и в
фикстуре `tests/fixtures/release-parity`. Он открывает pull request для найденных обновлений.
Уведомления об уязвимостях и pull request с исправлениями безопасности включены.

Для PR Dependabot workflow `dependabot-release-decision.yml` добавляет в описание раздел
`## Release` с решением `No release`. Существующее решение он не перезаписывает. Workflow
использует установленный rollout GitHub App с правом записи pull requests и запускает код из
доверенной базовой версии, а не из ветки PR. Изменение описания от App повторно запускает
проверку решения о релизе.

Автоматическое слияние запрашивается только для patch-обновления версии, направленного в `main`.
Workflow проверяет, что ни одна обновляемая зависимость не связана с Dependabot alert и что тип
обновления равен `version-update:semver-patch`. Только после обязательной проверки `Quality Gate`
GitHub может слить pull request. Исправления безопасности, minor- и major-обновления остаются на
ручной проверке.

Для проверки alert workflow использует secret `DEPENDABOT_ALERTS_TOKEN`. Это fine-grained token с
правом `Dependabot alerts: read`, ограниченный публичным репозиторием Quality Gate. Workflow читает
метаданные через GitHub API, но не checkout-ит и не запускает код pull request.

## Rollout после выпуска Quality Gate

После успешной публикации и проверки релиза release workflow отправляет событие с версией и точным
SHA переиспользуемого workflow. В
[публичном реестре потребителей](../.github/quality-gate-consumers.json) перечислены семь репозиториев:

- `quality-gate` использует `activation: "provider"`. Workflow `release-rollout.yml` создаёт один PR
  с обновлением policy, parity-фикстуры и связанных проверок;
- шесть приватных потребителей обрабатываются через `consumer-release-rollout.yml`. Для каждого
  создаётся или обновляется один PR, без второго PR в `quality-gate`.

В потребителях бот обновляет `quality-gate.toml`, если он есть, и все активные вызовы Quality Gate
workflows в `.github/workflows`. Для репозитория `skills` он также обновляет шаблон
`skills/setup-repo/assets/quality-workflow.yml`. Если выходит новый релиз, пока такой pull request
открыт, бот обновляет его до новой версии. Закрытый pull request того же релиза бот не открывает
снова; следующий релиз создаёт новый.

Все rollout pull request требуют решения человека и не сливаются автоматически. Бот не меняет
ветки по умолчанию. Если обновление одного потребителя не удалось, workflow сообщает об ошибке и
продолжает обработку остальных. Каждая задача получает отдельный токен для провайдера и одного
потребителя. Ошибка выдачи токена также не отменяет остальные задачи.

Перед изменениями бот проверяет версию и SHA по опубликованному релизу. Динамические ссылки вида
`@${{ vars.SHA }}`, отсутствующие настроенные ссылки и ветки без управляемого PR требуют
вмешательства владельца. Бот завершает обработку такого потребителя с ошибкой.

При потере события восстановите consumer rollout ручным запуском `consumer-release-rollout.yml`
с версией и SHA из записи успешной публикации. Для активации самого `quality-gate` отдельно запустите
`release-rollout.yml` с теми же данными.

Реестр находится в публичном репозитории, поэтому в нём видны имена всех зарегистрированных
репозиториев. Сами репозитории могут оставаться приватными.

## Доступы для GitHub App

Для создания pull request release workflow использует GitHub App, установленный только на
`quality-gate` и перечисленные в реестре репозитории. Ему требуются права `Contents: write` и
`Pull requests: write`; для потребителей добавляется `Workflows: write`, чтобы обновлять файлы
workflow. В настройках `quality-gate` хранятся repository variable
`QUALITY_GATE_ROLLOUT_APP_CLIENT_ID` и secret `QUALITY_GATE_ROLLOUT_APP_PRIVATE_KEY`.

В приватном репозитории на GitHub Free невозможно сделать статус CI обязательным условием слияния.
Поэтому rollout PR там всё равно откроется, но владелец должен проверить результаты Actions вручную
перед слиянием ([документация GitHub о доступности required checks](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches)).

## Граница установленной приёмки #137

На 28 октября 2026 года CodeRabbit установлен и, по сообщению владельца, комментирует PR.
На проверенном `main` с SHA `c193c3b8123b83e6d5142a47441fb3f4bb21bf33` ещё отсутствуют
`.coderabbit.yaml`, `dependabot-auto-merge.yml`, `release-rollout.yml` и
`consumer-release-rollout.yml`. Эти workflows находятся в `.github/workflows` текущей feature-ветки.
Активный ruleset для `main` требует только статус `Quality Gate`. Repository variable
`QUALITY_GATE_ROLLOUT_APP_CLIENT_ID` и secrets `QUALITY_GATE_ROLLOUT_APP_PRIVATE_KEY` и
`DEPENDABOT_ALERTS_TOKEN` существуют. Наличие secrets не подтверждает их работоспособность
или область установки приложения.

Codecov исключён владельцем из текущей реализации из-за ошибки входа GitHub 403.
Решение записано в [обсуждении #133](https://github.com/TheTraitor0FJesus/quality-gate/issues/133).
Конфигурация и загрузка покрытия отсутствуют, поэтому критерий Codecov из
[#137](https://github.com/TheTraitor0FJesus/quality-gate/issues/137) не выполнен.

После интеграции workflows остаётся подтвердить реальными событиями русский обзор CodeRabbit
без изменения описания PR, ожидание Quality Gate для Dependabot patch и ручное слияние остальных
обновлений, rollout провайдера и шести потребителей, обновление открытого rollout PR следующим
релизом, сохранение закрытого PR закрытым, отсутствие rollout при провале публикации и изоляцию
ошибки отдельного потребителя. Установленная приёмка остаётся незавершённой. Production rollout PR
и релизы специально ради получения тестового результата не создаются.
