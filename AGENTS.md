# Правила за разработка на проекта

1. Всички HTTP заявки към бекенда задължително минават през `api/client.py`.
2. Тестовете не правят реални мрежови повиквания (използвайте мокове или responses).
3. Преди всеки Pull Request, `ruff` и `pytest` трябва да минават без грешки. CI ги пуска на Python 3.11, на Linux и на Windows, с `pytest -W error::DeprecationWarning`.
4. Flet е фиксиран на `0.24.0`; диалозите и `SnackBar` се отварят и затварят през `page.open()`/`page.close()`.
5. Не опростявай кода за `user_id` и `settings.json` (`state/settings.py`, началото на `main.py`): `user_id` е единственият ключ към историята, паметта и файловете в бекенда, а възстановяването на повреден `settings.json` е умишлено. Нов `user_id` или `session_id` осиротява всичко старо.
6. Договорът с бекенда е в `docs/openapi.yaml`. Тялото на спомен е най-много 32 000 **байта** (кирилицата е 2 байта на буква), не букви.
7. Грешки и логове: всяка промяна в бекенда се записва на `INFO` в `api/client.py` (само id-та и размери), изгледът, който хваща грешка, я пише с `logger.error`/`warning`, показва `describe_error`/`error_detail` и пуска `asyncio.CancelledError` нагоре. В лога не влизат текст на съобщения и спомени, цели `user_id`/`session_id` и query string на заявка.
8. Справочникът на функциите е `function.md`. Обновявай го, когато добавяш, преименуваш или махаш функция.
9. Зависимости: границите са в `pyproject.toml`, точните версии — в `requirements-lock.txt` (един файл за Linux и Windows). CI инсталира от него (`pip install -r requirements-lock.txt`, после `pip install -e . --no-deps`). Променил си `pyproject.toml` или искаш нови версии — направи файла наново: `uv pip compile pyproject.toml --extra dev --universal --python-version 3.11 -o requirements-lock.txt`, после `ruff check .` и `pytest -W error::DeprecationWarning` в чист venv. Работният процес `Audit` пуска `pip-audit` върху файла при всяко качване и всеки понеделник; двата `--ignore-vuln` в `.github/workflows/audit.yml` (oauthlib, идва от Flet 0.24) се махат при Flet 1.x. Веднъж месечно: `pip list --outdated`.
