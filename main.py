import contextlib
import os
import random
import sqlite3
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests
import schedule
from camoufox import Camoufox
from dotenv import load_dotenv
from playwright._impl import _errors as pw_errors
from playwright.sync_api import Page

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
ACCOUNT_DELAY_MINUTES = int(os.getenv("ACCOUNT_DELAY_MINUTES", 30))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", 3))
TIMEOUT = int(os.getenv("TIMEOUT", 15000))
CHECK_INTERVAL_MINUTES = 10  # Интервал проверки аккаунтов


def log(message: str) -> None:
    """Логирование сообщений с временной меткой"""
    timestamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{timestamp}] {message}")


class AccountDB:
    """Класс для работы с базой данных аккаунтов"""

    def __init__(self, db_path: str = "accounts.db") -> None:
        self.conn = sqlite3.connect(db_path)
        self._create_table()

    def _create_table(self) -> None:
        """Создает таблицу accounts если она не существует"""
        with self.conn:
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    login_url TEXT NOT NULL,
                    password TEXT NOT NULL,
                    location TEXT NOT NULL,
                    last_check TIMESTAMP,
                    next_check TIMESTAMP,
                    is_blocked BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """
            )

    def add_account(self, login_url: str, password: str, location: str) -> None:
        """Добавляет новый аккаунт в базу данных"""
        with self.conn:
            self.conn.execute(
                "INSERT INTO accounts (login_url, password, location) VALUES (?, ?, ?, ?)",
                (login_url, password, location),
            )

    def get_active_accounts(self) -> list[dict]:
        """Возвращает список активных аккаунтов, готовых к проверке"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self.conn:
            cursor = self.conn.execute(
                """
                SELECT id, login_url, password, location, last_check, next_check, is_blocked 
                FROM accounts 
                WHERE is_blocked = FALSE OR next_check <= ?
                ORDER BY last_check ASC
            """,
                (now,),
            )

            accounts = []
            for row in cursor.fetchall():
                accounts.append(
                    {
                        "id": row[0],
                        "login_url": row[1],
                        "password": row[2],
                        "location": row[3],
                        "last_check": datetime.strptime(row[4], "%Y-%m-%d %H:%M:%S") if row[4] else None,
                        "next_check": datetime.strptime(row[5], "%Y-%m-%d %H:%M:%S") if row[5] else None,
                        "is_blocked": bool(row[6]),
                    }
                )
            return accounts

    def update_account_status(self, account_id: int, is_blocked: bool = False, next_check: datetime = None) -> None:
        """Обновляет статус аккаунта после проверки"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        next_check_str = next_check.strftime("%Y-%m-%d %H:%M:%S") if next_check else None

        with self.conn:
            self.conn.execute(
                """
                UPDATE accounts 
                SET last_check = ?, 
                    next_check = ?, 
                    is_blocked = ? 
                WHERE id = ?
            """,
                (now, next_check_str, is_blocked, account_id),
            )

    def close(self) -> None:
        """Закрывает соединение с базой данных"""
        self.conn.close()


class ConsoleMonitor:
    def __init__(self) -> None:
        self._429_detected = False
        self.console_messages = []

    def handle_console_message(self, msg):
        """Обработчик сообщений из консоли браузера"""

        message = str(msg.text)
        self.console_messages.append(message)

        # Проверяем признаки 429 ошибки
        error_indicators = ["429", "Too Many Requests", "CORS header", "Http failure response"]
        if any(indicator in message for indicator in error_indicators):
            log(f"Обнаружена 429 ошибка в консоли: {message}")
            self._429_detected = True


class AppointmentChecker:
    def __init__(self, db: AccountDB) -> None:
        self.db = db
        self.console_monitor = ConsoleMonitor()

    @staticmethod
    def save_error_page(page: Page, error_type: str = "error") -> tuple[None, None] | tuple[Path, Path]:
        """Сохраняет страницу в HTML и делает скриншот при ошибке"""

        timestamp = int(time.time())
        errors_dir = Path("errors")
        errors_dir.mkdir(exist_ok=True)
        html_filename = errors_dir / f"{error_type}_{timestamp}.html"
        screenshot_filename = errors_dir / f"{error_type}_{timestamp}.png"

        try:
            # Сохраняем HTML
            with open(html_filename, "w", encoding="utf-8") as f:
                f.write(page.content())
            log(f"Страница сохранена как {html_filename}")

            # Делаем скриншот
            page.screenshot(path=screenshot_filename)
            log(f"Скриншот сохранен как {screenshot_filename}")

            return html_filename, screenshot_filename
        except Exception as e:
            log(f"Ошибка при сохранении страницы: {e}")
            return None, None

    @staticmethod
    def send_telegram_notification(message: str, files: Optional[list[Path]] = None) -> None:
        """Отправляет уведомление в Telegram"""
        base_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

        try:
            # Отправка текстового сообщения
            response = requests.post(f"{base_url}/sendMessage", params={"chat_id": TELEGRAM_CHAT_ID, "text": message})
            response.raise_for_status()
            log("Уведомление в Telegram отправлено успешно")

            # Отправка файлов, если они есть
            for file in files:
                with open(file, "rb") as f:
                    response = requests.post(
                        f"{base_url}/sendDocument", data={"chat_id": TELEGRAM_CHAT_ID}, files={"document": f}
                    )
                    response.raise_for_status()
                    log(f"Файл {file} отправлен в Telegram")
        except Exception as e:
            log(f"Ошибка отправки в Telegram: {e}")

    def is_429_error(self, page: Page) -> bool:
        """Проверяет наличие 429 ошибки на странице или в консоли"""
        try:
            # Проверка текста страницы
            page_text = page.inner_text("body")
            error_indicators = ["429", "Too Many Requests"]
            if any(indicator in page_text for indicator in error_indicators):
                return True

            # Проверка через монитор консоли
            return self.console_monitor._429_detected
        except Exception:
            return False

    def handle_429_error(self, account: dict, page: Page) -> bool:
        """Обработка ситуации с 429 ошибкой"""

        html_file, screenshot_file = self.save_error_page(page, "429_error")

        # Устанавливаем время следующей проверки для этого аккаунта
        next_check = datetime.now() + timedelta(minutes=ACCOUNT_DELAY_MINUTES)
        self.db.update_account_status(account["id"], is_blocked=True, next_check=next_check)

        log(f"Обнаружена 429 ошибка. Откладываем аккаунт на {ACCOUNT_DELAY_MINUTES} минут")

        # Формируем сообщение с логами из консоли
        console_logs = "\n".join(self.console_monitor.console_messages[-10:])
        message = (
            f"⏳ Обнаружена 429 ошибка в аккаунте для {account['location']}. "
            f"Аккаунт приостановлен на {ACCOUNT_DELAY_MINUTES} мин\n"
            f"Последние логи консоли:\n{console_logs}"
        )

        if html_file and screenshot_file:
            message += f"\nФайлы для анализа:\nHTML: {html_file}\nСкриншот: {screenshot_file}"

        self.send_telegram_notification(message)
        self.console_monitor._429_detected = False
        return True

    def safe_click(self, page: Page, selector: str, timeout: int = TIMEOUT, retries: int = MAX_RETRIES) -> bool | None:
        """Безопасный клик с обработкой ошибок и повторными попытками"""

        for attempt in range(retries):
            try:
                # Проверка 429 ошибки перед действием
                if self.is_429_error(page):
                    return False

                element = page.wait_for_selector(selector, timeout=timeout)
                if not element:
                    log(f"Элемент {selector} не найден (попытка {attempt + 1}/{retries})")
                    continue

                element.click()
                time.sleep(1)  # Пауза после клика
                return True

            except Exception as e:
                error_msg = str(e)
                log(f"Ошибка при клике на {selector} (попытка {attempt + 1}/{retries}): {error_msg}")

                if attempt == retries - 1:
                    self.save_error_page(page, "click_error")
                    log(f"Не удалось кликнуть на {selector} после {retries} попыток")
                    raise e

                time.sleep(5)
        return None

    def perform_login_actions(self, page: Page, password: str) -> bool:
        """Выполняет последовательность действий для входа в систему"""

        actions = [
            ("#forceStart", "Нажатие кнопки 'Force Start'"),
            ("#password", "Ввод пароля", password),
            ("#submit", "Нажатие кнопки 'Submit'"),
            ("#appointmentBookingLink", "Переход к бронированию записи"),
            ("#book-appointment", "Начало процесса бронирования"),
            ("#onetrust-accept-btn-handler", "Принятие cookies"),
        ]

        for selector, description, *extra in actions:
            log(description)

            if selector == "#password":
                page.fill(selector, extra[0])
                continue

            if not self.safe_click(page, selector):
                return False

        return True

    def login(self, account: dict, page: Page) -> bool:
        """Процедура входа в аккаунт с улучшенной обработкой ошибок"""

        page.on("console", self.console_monitor.handle_console_message)
        location = account["location"]
        for attempt in range(MAX_RETRIES):
            try:
                log(f"Попытка входа {attempt + 1}/{MAX_RETRIES} для {location}")

                response = page.goto(account["login_url"], timeout=TIMEOUT)

                if not response or response.status == 429 or self.is_429_error(page):
                    log("Обнаружена 429 ошибка при загрузке страницы")
                    return self.handle_429_error(account, page)

                if not self.perform_login_actions(page, account["password"]):
                    continue

                return True

            except Exception as e:
                error_msg = str(e)
                log(f"Ошибка входа (попытка {attempt + 1}): {error_msg}")

                if "429" in error_msg or "Too Many Requests" in error_msg:
                    return self.handle_429_error(account, page)

                if attempt == MAX_RETRIES - 1:
                    html_file, screenshot_file = self.save_error_page(page, "login_error")
                    error_message = (
                        f"Ошибка входа после {MAX_RETRIES} попыток для {location}:\n"
                        f"{error_msg}\n{traceback.format_exc()}"
                    )
                    self.send_telegram_notification(
                        error_message, [html_file, screenshot_file] if html_file and screenshot_file else None
                    )
                    return False

                time.sleep(10)
        return False

    def check_location_availability(self, account: dict, page: Page) -> bool:
        """Проверяет доступность записи в указанной локации"""

        location = account["location"]
        try:
            log(f"Проверка локации {location}")
            self.safe_click(page, ".ng-input")

            self.safe_click(page, f"div.location-name:has-text('{location}')")

            log("Нажатие кнопки 'Продолжить'")
            self.safe_click(page, ".btn-lg")

            # Проверка модального окна
            with contextlib.suppress(pw_errors.TimeoutError):
                modal_text = page.locator("modal-container div.modal-content").text_content(timeout=2000)
                log(f"Текст попапа: {modal_text}")

                if "5002: No appointment slot" in modal_text:
                    log(f"Нет доступных слотов в {location}")
                    return False

            # Проверка доступности выбора времени
            time_slots = page.locator("li.btn.btn-link.appointment-btn:not(.slotselected)")
            count = time_slots.count()

            if count == 0:
                log(f"Нет доступных временных слотов в {location}")
                return False

            log(f"!!! НАЙДЕНА СВОБОДНАЯ ЗАПИСЬ в {location} !!!")

            # Получение информации о дате и времени
            date_element = page.locator("button.date-item.selected").text_content()
            current_year = datetime.now().year
            slot_date = datetime.strptime(f"{date_element} {current_year}", "%a %d %b %Y")

            # Выбор случайного слота
            random_slot = random.randint(0, count - 1)
            selected_slot = time_slots.nth(random_slot)

            slot_time = selected_slot.locator("span.slot").text_content()
            available_slots = selected_slot.locator("span.customer-nos span.tool-link").text_content()

            log(f"Выбрана дата: {slot_date}, время: {slot_time}, доступно слотов: {available_slots}")

            selected_slot.click()

            self.safe_click(page, "button.btn.btn-primary-vfs.btn-lg")
            log("Успешно выбрано время и нажата кнопка Continue")

            self.safe_click(page, "button.btn.btn-primary-vfs.btn-lg")
            log("Нажата кнопка Continue на втором этапе")

            self.safe_click(page, "#read-agree")
            self.safe_click(page, "button.ng-star-inserted")
            log("Пройден третий этап записи")

            # Сохраняем последнюю страницу
            html = page.content()
            filename = f"available_{location.replace(' ', '_')}_{int(time.time())}.html"
            with open(filename, "w", encoding="utf-8") as f:
                f.write(html)
            log(f"Страница сохранена как {filename}")

            message = f"🚀 Удачная запись в {location}! Файл: {filename}"
            self.send_telegram_notification(message)
            time.sleep(1)
            return True

        except Exception as e:
            log(f"Ошибка при проверке {location}: {str(e)}")
            self.save_error_page(page, f"location_error_{location}")
            if "429" in str(e) or "Too Many Requests" in str(e):
                self.handle_429_error(account, page)
            return False

    def check_account(self, account: dict) -> None:
        """Проверяет один аккаунт"""
        with Camoufox(humanize=True, headless=os.getenv("HEADLESS")) as browser:
            page = browser.new_page()
            try:
                location = account["location"]
                self.db.update_account_status(account["id"])

                if not self.login(account, page):
                    log(f"Не удалось войти в аккаунт для {location}")
                    self.send_telegram_notification(f"⚠️ Не удалось войти в аккаунт для {location}")
                    return

                if self.check_location_availability(account, page):
                    return

            except Exception as e:
                log(f"Критическая ошибка при проверке {location}: {str(e)}")
                html_file, screenshot_file = self.save_error_page(page, "critical_error")
                error_message = (
                    f"🔥 Критическая ошибка в скрипте для {location}:\n" f"{str(e)}\n{traceback.format_exc()}"
                )
                self.send_telegram_notification(
                    error_message, [html_file, screenshot_file] if html_file and screenshot_file else None
                )

    def run_check(self) -> None:
        """Запускает проверку всех активных аккаунтов"""
        log("=== Начало проверки аккаунтов ===")
        accounts = self.db.get_active_accounts()

        if not accounts:
            log("Нет активных аккаунтов для проверки")
            return

        for account in accounts:
            log(f"\nПроверка аккаунта для {account['location']}")
            self.check_account(account)
            time.sleep(5)  # Небольшая пауза между аккаунтами

        log("=== Проверка аккаунтов завершена ===")


def main():
    """Основная функция запуска приложения"""
    db = AccountDB()
    checker = AppointmentChecker(db)

    # Добавляем тестовые аккаунты, если их нет в базе
    if not db.get_active_accounts():
        db.add_account(
            os.getenv("ACCOUNT1_LOGIN_URL"),
            os.getenv("ACCOUNT1_PASSWORD"),
            "Moscow",
        )
        db.add_account(
            os.getenv("ACCOUNT2_LOGIN_URL"),
            os.getenv("ACCOUNT2_PASSWORD"),
            "St Petersburg",
        )

    # Настраиваем периодическую проверку
    schedule.every(CHECK_INTERVAL_MINUTES).minutes.do(checker.run_check)

    # Первый запуск сразу
    checker.run_check()

    # Бесконечный цикл для работы планировщика
    try:
        while True:
            schedule.run_pending()
            time.sleep(1)
    except KeyboardInterrupt:
        db.close()
        log("Скрипт остановлен")


if __name__ == "__main__":
    main()
