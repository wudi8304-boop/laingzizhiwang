#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cron 入口：仅调用统一监控服务，业务逻辑位于 services.monitor。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import Database
from migrate_data import migrate
from services.monitor import MonitorBusyError, MonitorService


def main():
    db = Database()
    db.initialize()
    migrate(db)
    result = MonitorService(db).run_if_due()
    if not result.get("skipped"):
        print(json.dumps(result, ensure_ascii=False), flush=True)
    return result


if __name__ == "__main__":
    try:
        main()
    except MonitorBusyError:
        pass
    except Exception as exc:
        print("监控执行失败: %s" % exc, file=sys.stderr, flush=True)
        raise
