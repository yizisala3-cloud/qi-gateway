"""Shared clocks and patch contexts for planning behaviour tests."""

from datetime import datetime, timedelta, timezone
from unittest import mock
from unittest.mock import patch

from gateway import planning, planning_recompute, planning_runtime
from .planning_db import CoreClient, IdentityDatabase

MODULE = "gateway.planning"
CST = timezone(timedelta(hours=8))


def cst(*args) -> datetime:
    return datetime(*args, tzinfo=CST)


def setup_core(module_now=None):
    """返回 (client, contextmanager)，统一 patch get_client、时间与 app_settings。"""
    client = CoreClient()

    def fake_load_setting(key):
        for row in client.rows["app_settings"]:
            if row.get("key") == key:
                return row.get("value")
        return None

    def fake_save_setting(key, value):
        for row in client.rows["app_settings"]:
            if row.get("key") == key:
                row["value"] = value
                return True
        client.rows["app_settings"].append({"key": key, "value": value})
        return True

    class _Patches:
        def __init__(self, now):
            self._now = now
            self._tokens = []

        def __enter__(self):
            self._tokens.append(patch("gateway.planning_runtime.get_client", return_value=client))
            self._tokens.append(patch(f"{MODULE}.db.load_app_setting", fake_load_setting))
            self._tokens.append(patch(f"{MODULE}.db.save_app_setting", fake_save_setting))
            if self._now is not None:
                self._tokens.append(patch.object(planning_runtime, "_now", lambda: self._now))
            for token in self._tokens:
                token.start()
            return client

        def __exit__(self, *exc):
            for token in reversed(self._tokens):
                token.stop()
            return False

    return client, (lambda now=module_now: _Patches(now))



def at(day, hour=7, minute=0, month=9):
    return datetime(2026, month, day, hour, minute, tzinfo=CST)


class Context:
    def __init__(self):
        self.db = IdentityDatabase()
        self.settings = {}
        self.patches = [
            mock.patch.object(planning_runtime, "get_client", return_value=self.db),
            mock.patch.object(planning.db, "load_app_setting", side_effect=self.settings.get),
            mock.patch.object(planning.db, "save_app_setting", side_effect=self.save),
            mock.patch.object(planning_recompute, "request_recompute"),
        ]

    def save(self, key, value):
        self.settings[key] = value
        return True

    def __enter__(self):
        for patch in self.patches:
            patch.start()
        return self

    def __exit__(self, *_):
        for patch in reversed(self.patches):
            patch.stop()

    @property
    def rows(self):
        return self.db.rows["planning_occurrence"]

    def create(self, kind, now=at(24), **kwargs):
        return planning.create_task({
            "content": kind, "task_type": kind, "estimated_minutes": 30, **kwargs,
        }, now)


class CreationContext(Context):
    """Creation scenarios also inspect task rows directly."""

    @property
    def tasks(self):
        return self.db.rows["planning_task"]
