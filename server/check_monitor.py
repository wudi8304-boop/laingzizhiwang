#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cron 入口：仅调用统一监控服务，业务逻辑位于 services.monitor。"""
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import Database
from migrate_data import migrate
from services.account import AccountService
from services.monitor import MonitorBusyError, MonitorService


def main(at=None):
    scheduled_at = at or datetime.now()
    db = Database()
    db.initialize()
    migrate(db)
    results = {}
    try:
        results["account"] = AccountService(db).run_if_due(scheduled_at)
        if not results["account"].get("skipped"):
            print(json.dumps({"account": results["account"]}, ensure_ascii=False), flush=True)
    except Exception as exc:
        results["account"] = {"status": "failed", "error": str(exc)}
        print("自动签到执行失败: %s" % exc, file=sys.stderr, flush=True)
    results["monitor"] = MonitorService(db).run_if_due(scheduled_at)
    if not results["monitor"].get("skipped"):
        print(json.dumps({"monitor": results["monitor"]}, ensure_ascii=False), flush=True)
    return results


if __name__ == "__main__":
    try:
        main()
    except MonitorBusyError:
        pass
    except Exception as exc:
        print("监控执行失败: %s" % exc, file=sys.stderr, flush=True)
        raise
