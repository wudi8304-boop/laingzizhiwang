#!/usr/bin/env node
"use strict";

const assert = require("assert");
const { normalizeVendorRecord, payloadItems } = require("../vendor_scrape");

const record = normalizeVendorRecord(
  { id: 7, subject: "测试公司", name: "旧列表名称", appid: "wx-list" },
  {
    appName: "详情名称",
    logo_path: "/uploads/avatar.png",
    intro: "小程序简介",
    categories: [{ name: "工具" }, { name: "生活" }],
    wx_username: "gh_demo",
    manager: "管理员",
  },
  { data: { app_secret: "secret-value", admin_email: "vendor@example.com" } },
  "https://xcx.qn76.cn/apps"
);

assert.strictEqual(record.externalId, "7");
assert.strictEqual(record.companyName, "测试公司");
assert.strictEqual(record.miniProgramName, "详情名称");
assert.strictEqual(record.avatarUrl, "https://xcx.qn76.cn/uploads/avatar.png");
assert.strictEqual(record.description, "小程序简介");
assert.strictEqual(record.category, "工具、生活");
assert.strictEqual(record.originalId, "gh_demo");
assert.strictEqual(record.secret, "secret-value");
assert.strictEqual(record.email, "vendor@example.com");
assert.deepStrictEqual(payloadItems({ data: { apps: [{ id: 1 }] } }), [{ id: 1 }]);

console.log("vendor_scrape_fixture_ok");
