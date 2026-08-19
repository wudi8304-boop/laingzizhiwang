#!/usr/bin/env node
"use strict";

const { chromium } = require("playwright");

(async () => {
  const password = process.env.APP_PASSWORD;
  if (!password) throw new Error("APP_PASSWORD is required");
  const browser = await chromium.launch({ headless: true, channel: "chromium" });
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on("pageerror", error => errors.push(String(error)));
    await page.goto(process.env.APP_URL || "http://127.0.0.1:8080/", {
      waitUntil: "domcontentloaded",
      timeout: 30000,
    });
    await page.fill("#loginUser", process.env.APP_USERNAME || "admin");
    await page.fill("#loginPwd", password);
    await page.click("#loginMask button");
    await page.waitForSelector("#layout", { state: "visible", timeout: 30000 });
    await page.waitForSelector("#page-dashboard.active", { timeout: 10000 });
    const metricValues = await page.evaluate(() => {
      const now = new Date();
      const previous = new Date(now.getFullYear(), now.getMonth() - 1, 1);
      const stamp = (date, day) =>
        `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(day).padStart(2, "0")} 09:00:00`;
      renderMonthlyStats([
        { status: "备案完成", completionTime: stamp(previous, 5), settlementTime: stamp(previous, 4) },
        { status: "已结算", completionTime: stamp(previous, 6), settlementTime: stamp(now, 1) },
        { status: "备案完成", completionTime: stamp(now, 2), settlementTime: "" },
        { status: "备案中", completionTime: stamp(now, 3), settlementTime: "" },
      ], now);
      const values = Array.from(document.querySelectorAll("#monthlyStats .num")).map(el => el.textContent);
      renderDashboard();
      return values;
    });
    if (JSON.stringify(metricValues) !== JSON.stringify(["2", "1", "1"])) {
      throw new Error("unexpected monthly metric values: " + JSON.stringify(metricValues));
    }
    const monthlyCards = await page.locator("#monthlyStats .stat-card").count();
    if (monthlyCards !== 3) throw new Error("unexpected monthly card count: " + monthlyCards);
    await page.locator("#monthlyStats .stat-card").first().click();
    await page.waitForSelector("#monthlyDetailModal.show", { timeout: 10000 });
    await page.locator("#monthlyDetailModal .close").click();
    await page.click('.nav-item[data-page="companies"]');
    await page.waitForSelector("#page-companies.active", { timeout: 10000 });
    const cards = await page.locator("#companyCards .company-card").count();
    const role = await page.locator("#currentUserLabel").innerText();
    if (!role.includes("总管理员")) throw new Error("unexpected role label: " + role);
    await page.click('.nav-item[data-page="monitor"]');
    await page.waitForSelector("#page-monitor.active", { timeout: 10000 });
    if (!await page.locator("#accountCheckinEnabled").count()) {
      throw new Error("daily checkin toggle missing");
    }
    await page.click('.nav-item[data-page="data"]');
    await page.waitForSelector("#page-data.active", { timeout: 10000 });
    const headers = await page.locator("#thead th").allInnerTexts();
    if (!headers.some(text => text.includes("头像"))) throw new Error("avatar column missing");
    if (!headers.some(text => text.includes("法人手机号"))) throw new Error("legal phone column missing");
    if (!headers.some(text => text.includes("小程序手机号"))) throw new Error("program phone column missing");
    if (headers.some(text => text.includes("小程序密码"))) throw new Error("password column should be hidden");
    const lockedStatus = page.locator("#tbody .tag-status-已结算").first();
    if (await lockedStatus.count()) {
      const lockedRow = lockedStatus.locator("xpath=ancestor::tr");
      if (!await lockedRow.locator('input[type="checkbox"]').isDisabled()) {
        throw new Error("settled program checkbox should be disabled");
      }
      if (await lockedRow.locator("button", { hasText: "编辑" }).count()) {
        throw new Error("settled program edit action should be hidden");
      }
    }
    const statuses = await page.locator("#bulkStatus option").allTextContents();
    const expectedStatuses = ["批量改状态", "待注册", "待审核", "备案中", "备案完成", "已结算"];
    if (JSON.stringify(statuses) !== JSON.stringify(expectedStatuses)) {
      throw new Error("unexpected statuses: " + JSON.stringify(statuses));
    }
    await page.locator("#page-data button", { hasText: "+ 新增" }).click();
    await page.waitForSelector("#editModal.show", { timeout: 10000 });
    if (await page.locator("#f_status").inputValue() !== "待注册") {
      throw new Error("new program default status is not 待注册");
    }
    await page.locator("#editModal .close").click();
    const avatarImages = await page.locator("#tbody img.avatar-thumb").count();
    await page.locator("#tbody button", { hasText: "编辑" }).first().click();
    await page.waitForSelector("#editModal.show", { timeout: 10000 });
    if (await page.locator("#f_avatarUrl").count()) {
      throw new Error("avatar url input should be hidden");
    }
    if (!await page.locator("#avatarPreviewBox").count()) {
      throw new Error("avatar preview missing");
    }
    for (const selector of ["#f_legalPersonPhone", "#f_miniProgramPhone", "#f_miniProgramPassword", "#f_description", "#f_category"]) {
      if (!await page.locator(selector).count()) throw new Error("edit field missing: " + selector);
    }
    if (!await page.locator("#editModal button", { hasText: "刷新换邮箱" }).count()) {
      throw new Error("refresh email action missing");
    }
    await page.locator("#editModal .close").click();
    if (errors.length) throw new Error("page errors: " + errors.join("; "));
    console.log(JSON.stringify({ ok: true, monthlyCards, companyCards: cards, avatarImages, role }));
  } finally {
    await browser.close();
  }
})().catch(error => {
  console.error(error.stack || String(error));
  process.exit(1);
});
