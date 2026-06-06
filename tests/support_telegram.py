from __future__ import annotations

import sys
import types


class FakeApplication:
    def add_handler(self, handler) -> None:
        pass

    def run_polling(self) -> None:
        pass


class FakeApplicationBuilder:
    def token(self, token):
        return self

    def build(self):
        return FakeApplication()


class FakeBot:
    async def send_message(self, *, chat_id, text) -> None:
        pass


class FakeFilters:
    TEXT = object()


class FakeContextTypes:
    DEFAULT_TYPE = object


def install() -> None:
    telegram = types.ModuleType("telegram")
    telegram.Bot = FakeBot
    telegram.Update = object

    telegram_ext = types.ModuleType("telegram.ext")
    telegram_ext.ApplicationBuilder = FakeApplicationBuilder
    telegram_ext.MessageHandler = lambda filters, handle: (filters, handle)
    telegram_ext.filters = FakeFilters
    telegram_ext.ContextTypes = FakeContextTypes

    sys.modules.setdefault("telegram", telegram)
    sys.modules.setdefault("telegram.ext", telegram_ext)
