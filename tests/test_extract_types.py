"""Тесты для extract_types() — определение типа и уровня (цвета) тревоги.

Покрывает ответы API ukrainealarm:
  - AIR с alertLevel Red/Orange/Yellow -> AIR_RED / AIR_ORANGE / AIR_YELLOW
  - AIR без уровня или с пустым alertLevel -> AIR (общая тревога)
  - старый формат с ключом level (fallback)
  - не-воздушные типы: ARTILLERY, URBAN_FIGHTS и т.д.
  - несколько типов одновременно
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alarm import extract_types  # noqa: E402


def region(alerts):
    """Оборачивает список тревог в структуру ответа API ukrainealarm."""
    return [{"District": "Тест", "activeAlerts": alerts}]


class TestExtractTypes(unittest.TestCase):
    def test_air_red(self):
        self.assertEqual(
            extract_types(region([
                {"type": "AIR", "activeAlertLevels": [{"alertLevel": "Red", "reason": ""}]}
            ])),
            ["AIR_RED"],
        )

    def test_air_orange(self):
        self.assertEqual(
            extract_types(region([
                {"type": "AIR", "activeAlertLevels": [{"alertLevel": "Orange", "reason": ""}]}
            ])),
            ["AIR_ORANGE"],
        )

    def test_air_yellow(self):
        self.assertEqual(
            extract_types(region([
                {"type": "AIR", "activeAlertLevels": [{"alertLevel": "Yellow", "reason": ""}]}
            ])),
            ["AIR_YELLOW"],
        )

    def test_air_no_level(self):
        """Уровня нет — общая тревога AIR."""
        self.assertEqual(
            extract_types(region([
                {"type": "AIR", "activeAlertLevels": [{"alertLevel": "", "reason": ""}]}
            ])),
            ["AIR"],
        )

    def test_air_empty_levels(self):
        """activeAlertLevels пуст — общая тревога AIR."""
        self.assertEqual(
            extract_types(region([
                {"type": "AIR", "activeAlertLevels": []}
            ])),
            ["AIR"],
        )

    def test_air_missing_levels_key(self):
        """Ключа activeAlertLevels нет вообще — общая тревога AIR."""
        self.assertEqual(
            extract_types(region([
                {"type": "AIR"}
            ])),
            ["AIR"],
        )

    def test_air_old_level_key(self):
        """Fallback на старый ключ 'level'."""
        self.assertEqual(
            extract_types(region([
                {"type": "AIR", "activeAlertLevels": [{"level": "RED", "reason": ""}]}
            ])),
            ["AIR_RED"],
        )

    def test_artillery(self):
        self.assertEqual(
            extract_types(region([
                {"type": "ARTILLERY", "activeAlertLevels": []}
            ])),
            ["ARTILLERY"],
        )

    def test_urban_fights(self):
        self.assertEqual(
            extract_types(region([
                {"type": "URBAN_FIGHTS", "activeAlertLevels": []}
            ])),
            ["URBAN_FIGHTS"],
        )

    def test_mixed_types(self):
        """Несколько типов — возвращаются все уникальные в порядке поступления."""
        self.assertEqual(
            extract_types(region([
                {"type": "AIR", "activeAlertLevels": [{"alertLevel": "Red"}]},
                {"type": "ARTILLERY", "activeAlertLevels": []}
            ])),
            ["AIR_RED", "ARTILLERY"],
        )

    def test_empty_response(self):
        self.assertEqual(extract_types([]), [])
        self.assertEqual(extract_types(None), [])
        self.assertEqual(extract_types([{}]), [])


if __name__ == "__main__":
    unittest.main()
