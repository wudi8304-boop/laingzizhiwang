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
    await page.click('.nav-item[data-page="companies"]');
    await page.waitForSelector("#page-companies.active", { timeout: 10000 });
    const cards = await page.locator("#companyCards .company-card").count();
    const role = await page.locator("#currentUserLabel").innerText();
    if (!role.includes("总管理员")) throw new Error("unexpected role label: " + role);
    await page.click('.nav-item[data-page="data"]');
    await page.waitForSelector("#page-data.active", { timeout: 10000 });
    const headers = await page.locator("#thead th").allInnerTexts();
    if (!headers.some(text => text.includes("头像"))) throw new Error("avatar column missing");
    if (headers.some(text => text.includes("小程序密码"))) throw new Error("password column should be hidden");
    const avatarImages = await page.locator("#tbody img.avatar-thumb").count();
    await page.locator("#tbody button", { hasText: "编辑" }).first().click();
    await page.waitForSelector("#editModal.show", { timeout: 10000 });
    for (const selector of ["#f_avatarUrl", "#f_miniProgramPassword", "#f_description", "#f_category"]) {
      if (!await page.locator(selector).count()) throw new Error("edit field missing: " + selector);
    }
    await page.locator("#editModal .close").click();
    if (errors.length) throw new Error("page errors: " + errors.join("; "));
    console.log(JSON.stringify({ ok: true, companyCards: cards, avatarImages, role }));
  } finally {
    await browser.close();
  }
})().catch(error => {
  console.error(error.stack || String(error));
  process.exit(1);
});
