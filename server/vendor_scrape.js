#!/usr/bin/env node
'use strict';

const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

const output = process.argv[2];

const env = (name, fallback = '') => (process.env[name] || '').trim() || fallback;
const map = new Map([
  ['小程序名称', 'miniProgramName'], ['名称', 'miniProgramName'], ['小程序名字', 'miniProgramName'],
  ['appid', 'appid'], ['app id', 'appid'], ['小程序appid', 'appid'],
  ['原始id', 'originalId'], ['原始 id', 'originalId'],
  ['公司', 'companyName'], ['主体', 'companyName'], ['管理员', 'admin'],
  ['邮箱', 'email'], ['状态', 'status'], ['简介', 'description'], ['类目', 'category'],
]);
const normalize = value => String(value || '').trim().replace(/\s+/g, ' ').toLowerCase();
const objectValue = value => value && typeof value === 'object' && !Array.isArray(value) ? value : {};
const unwrap = payload => {
  const root = objectValue(payload);
  return objectValue(root.app || root.data || root.detail || root.result || root);
};
const firstValue = (objects, keys) => {
  for (const object of objects.map(unwrap)) {
    for (const key of keys) {
      const value = object[key];
      if (value !== undefined && value !== null && String(value).trim() !== '') return value;
    }
  }
  return '';
};
const textValue = value => {
  if (Array.isArray(value)) {
    return value.map(item => {
      if (item && typeof item === 'object') return item.name || item.title || item.label || '';
      return item;
    }).filter(Boolean).join('、');
  }
  if (value && typeof value === 'object') return value.name || value.title || value.label || '';
  return String(value || '').trim();
};
const payloadItems = payload => {
  if (Array.isArray(payload)) return payload;
  const root = objectValue(payload);
  const data = objectValue(root.data);
  return root.apps || root.companies || root.items || root.list ||
    data.apps || data.companies || data.items || data.list || [];
};
const normalizeVendorRecord = (app, details, secrets, baseUrl) => {
  const sources = [details, app];
  let avatarUrl = textValue(firstValue(sources, [
    'avatar_url', 'avatarUrl', 'avatar', 'logo_path', 'logo_url', 'logoUrl', 'logo',
    'wx_headimg', 'icon', 'head_img', 'headImg',
  ]));
  if (avatarUrl && !/^(?:https?:|data:|blob:)/i.test(avatarUrl)) {
    try { avatarUrl = new URL(avatarUrl, baseUrl).toString(); } catch (_) {}
  }
  return {
    externalId: String(firstValue(sources, ['id']) || app.id || ''),
    companyName: textValue(firstValue(sources, ['subject', 'company_name', 'companyName', 'company'])),
    miniProgramName: textValue(firstValue(sources, ['name', 'app_name', 'appName', 'mini_program_name'])),
    avatarUrl,
    description: textValue(firstValue(sources, [
      'description', 'desc', 'intro', 'introduction', 'introduce', 'profile',
    ])),
    category: textValue(firstValue(sources, [
      'category', 'categories', 'service_category', 'serviceCategory', 'category_name', 'categoryName',
    ])),
    appid: textValue(firstValue(sources, ['appid', 'app_id', 'appId'])),
    originalId: textValue(firstValue(sources, ['wx_username', 'original_id', 'originalId'])),
    secret: textValue(firstValue([secrets], ['appSecret', 'app_secret', 'secret'])),
    admin: textValue(firstValue(sources, ['manager', 'admin', 'administrator'])),
    email: textValue(firstValue([secrets, details, app], ['email', 'admin_email', 'adminEmail'])),
  };
};

async function main() {
  if (!output) throw new Error('用法: node vendor_scrape.js <output.json>');
  const username = env('VENDOR_USERNAME');
  const password = process.env.VENDOR_PASSWORD || '';
  if (!username || !password) throw new Error('未配置 VENDOR_USERNAME/VENDOR_PASSWORD');
  const debugDir = path.join(env('LAINGZIZHIWANG_DATA_DIR', '/www/laingzizhiwang-data'), 'vendor-debug');
  fs.mkdirSync(debugDir, { recursive: true });
  const executablePath = env('VENDOR_BROWSER_EXECUTABLE');
  const browser = await chromium.launch({
    headless: env('VENDOR_HEADLESS', 'true') !== 'false',
    executablePath: executablePath || undefined,
    channel: executablePath ? undefined : env('VENDOR_BROWSER_CHANNEL', 'chromium'),
  });
  const page = await browser.newPage({ locale: 'zh-CN' });
  const responseLog = [];
  let companiesPayload = null;
  let appsPayload = null;
  let secretsPayload = null;
  const requestLog = [];
  const consoleLog = [];
  page.on('request', request => {
    const type = request.resourceType();
    if (type === 'xhr' || type === 'fetch') {
      requestLog.push({ method: request.method(), url: request.url() });
    }
  });
  page.on('requestfailed', request => {
    requestLog.push({ failed: true, method: request.method(), url: request.url(), error: request.failure() });
  });
  page.on('console', message => {
    if (message.type() === 'error' || message.type() === 'warning') {
      consoleLog.push({ type: message.type(), text: message.text().slice(0, 500) });
    }
  });
  page.on('pageerror', error => consoleLog.push({ type: 'pageerror', text: error.message }));
  page.on('response', async response => {
    const type = response.request().resourceType();
    if (type === 'xhr' || type === 'fetch') {
      responseLog.push({ status: response.status(), method: response.request().method(), url: response.url() });
      if (response.status() === 200 && /\/api\/companies(?:\?|$)/.test(response.url())) {
        try { companiesPayload = await response.json(); } catch (_) {}
      }
      if (response.status() === 200 && /\/api\/apps(?:\?|$)/.test(response.url())) {
        try { appsPayload = await response.json(); } catch (_) {}
      }
      if (response.status() === 200 && /\/api\/apps\/\d+\/secrets(?:\?|$)/.test(response.url())) {
        try { secretsPayload = await response.json(); } catch (_) {}
      }
    }
  });
  try {
    await page.goto(env('VENDOR_BASE_URL', 'https://xcx.qn76.cn/'), {
      waitUntil: 'domcontentloaded', timeout: 60000,
    });
    await page.locator(env(
      'VENDOR_USERNAME_SELECTOR',
      "input[placeholder*='账号'],input[placeholder*='用户名'],input[type='text']"
    )).first().fill(username);
    await page.locator(env(
      'VENDOR_PASSWORD_SELECTOR', "input[placeholder*='密码'],input[type='password']"
    )).first().fill(password);
    await page.locator(env(
      'VENDOR_LOGIN_SELECTOR', "button:has-text('登录'),button[type='submit'],input[type='submit']"
    )).first().click();
    await page.waitForLoadState('networkidle', { timeout: 60000 }).catch(() => {});
    await page.waitForFunction(
      () => !document.querySelector('button.ant-btn-loading'), null, { timeout: 60000 }
    ).catch(() => {});
    if (env('VENDOR_INSPECT', 'false') === 'true') {
      console.log(JSON.stringify({
        inspect: true,
        url: page.url(),
        title: await page.title(),
        headings: await page.locator('h1,h2,h3').allInnerTexts(),
        buttons: await page.locator('button').allInnerTexts(),
        inputs: await page.locator('input').evaluateAll(nodes => nodes.map(node => ({
          type: node.type, name: node.name, placeholder: node.placeholder,
          valueLength: node.value.length, disabled: node.disabled,
        }))),
        buttonHtml: await page.locator('button').evaluateAll(nodes => nodes.map(node => node.outerHTML)),
        links: (await page.locator('a').allInnerTexts()).slice(0, 50),
        requests: requestLog,
        responses: responseLog,
        console: consoleLog,
        text: (await page.locator('body').innerText()).slice(0, 3000),
      }));
    }
    if (env('VENDOR_LIST_URL')) {
      await page.goto(env('VENDOR_LIST_URL'), { waitUntil: 'networkidle', timeout: 60000 });
    } else {
      const companyNav = page.getByText('公司列表', { exact: true }).last();
      if (await companyNav.count()) {
        await companyNav.click();
        await page.waitForTimeout(1000);
      }
    }
    for (let i = 0; i < 60 && !companiesPayload; i += 1) await page.waitForTimeout(250);
    if (env('VENDOR_INSPECT', 'false') === 'true') {
      console.log(JSON.stringify({
        inspect: 'company-list',
        url: page.url(),
        companiesApi: companiesPayload,
        storageKeys: await page.evaluate(() => Object.keys(localStorage)),
        tables: await page.locator('table').evaluateAll(nodes => nodes.map(node => node.innerText.slice(0, 3000))),
        buttons: await page.locator('button').allInnerTexts(),
        requests: requestLog,
        responses: responseLog,
      }));
      const programNav = page.getByText('小程序列表', { exact: true }).last();
      if (await programNav.count()) {
        await programNav.click();
        await page.waitForTimeout(1000);
        const appItems = payloadItems(appsPayload);
        const firstId = Array.isArray(appItems) && appItems[0] ? appItems[0].id : null;
        const detailShapes = firstId ? await page.evaluate(async id => {
          const token = localStorage.getItem('kc_token');
          const headers = token ? { Authorization: `Bearer ${token}` } : {};
          const urls = [`/api/apps/${id}`, `/api/apps/${id}/detail`, `/api/apps/${id}/credentials`];
          const result = [];
          for (const url of urls) {
            const response = await fetch(url, { headers });
            let body = null;
            try { body = await response.json(); } catch (_) {}
            const value = body && (body.app || body.data || body);
            result.push({
              url, status: response.status,
              keys: value && typeof value === 'object' && !Array.isArray(value) ? Object.keys(value) : [],
            });
          }
          return result;
        }, firstId) : [];
        const completeButton = page.getByText('完善信息', { exact: true }).first();
        if (await completeButton.count()) {
          await completeButton.click();
          await page.waitForTimeout(1000);
        }
        console.log(JSON.stringify({
          inspect: 'program-list',
          url: page.url(),
          appsShape: appsPayload ? {
            keys: Object.keys(appsPayload),
            itemCount: Array.isArray(appsPayload) ? appsPayload.length :
              (Array.isArray(appsPayload.apps) ? appsPayload.apps.length : null),
            itemKeys: (() => {
              const items = Array.isArray(appsPayload) ? appsPayload : appsPayload.apps;
              return Array.isArray(items) && items[0] ? Object.keys(items[0]) : [];
            })(),
          } : null,
          detailShapes: detailShapes,
          formLabels: await page.locator('.ant-modal label,.ant-drawer label').allInnerTexts(),
          formInputCount: await page.locator('.ant-modal input,.ant-drawer input').count(),
          secretsShape: secretsPayload ? {
            keys: Object.keys(secretsPayload),
            nestedKeys: Object.fromEntries(Object.entries(secretsPayload)
              .filter(([, value]) => value && typeof value === 'object' && !Array.isArray(value))
              .map(([key, value]) => [key, Object.keys(value)])),
          } : null,
          requests: requestLog,
          responses: responseLog,
        }));
      }
      throw new Error('诊断模式完成');
    }
    const companyItems = payloadItems(companiesPayload);
    if (!Array.isArray(companyItems)) throw new Error('乙方平台公司接口响应格式不正确');
    const mode = env('VENDOR_MODE', 'sync');
    const knownFile = env('VENDOR_KNOWN_COMPANIES_FILE');
    const known = knownFile && fs.existsSync(knownFile) ?
      JSON.parse(fs.readFileSync(knownFile, 'utf8')).map(normalize).filter(x => x.length >= 4) : [];
    const isKnown = name => {
      const value = normalize(name);
      return known.some(alias => value === alias || value.includes(alias) || alias.includes(value));
    };
    const newCompanies = mode === 'match' ? [] :
      companyItems.map(item => String(item.name || '').trim()).filter(name => name && !isKnown(name));
    const matchFile = env('VENDOR_MATCH_CANDIDATES_FILE');
    const candidates = mode === 'match' && matchFile && fs.existsSync(matchFile) ?
      JSON.parse(fs.readFileSync(matchFile, 'utf8')) : [];
    if ((mode === 'match' && !candidates.length) || (mode !== 'match' && !newCompanies.length)) {
      fs.writeFileSync(output, '[]', 'utf8');
      console.log(JSON.stringify({ mode, count: 0, newCompanies: [] }));
      return;
    }

    const programNav = page.getByText('小程序列表', { exact: true }).last();
    if (!await programNav.count()) throw new Error('未找到“小程序列表”导航');
    await programNav.click();
    for (let i = 0; i < 60 && !appsPayload; i += 1) await page.waitForTimeout(250);
    const appItems = payloadItems(appsPayload);
    if (!Array.isArray(appItems)) throw new Error('乙方平台小程序接口响应格式不正确');
    if (env('VENDOR_SHAPE_ONLY', 'false') === 'true') {
      const sample = objectValue(appItems[0]);
      console.log(JSON.stringify({
        shapeOnly: true,
        itemCount: appItems.length,
        itemKeys: Object.keys(sample),
        nestedKeys: Object.fromEntries(Object.entries(sample)
          .filter(([, value]) => value && typeof value === 'object' && !Array.isArray(value))
          .map(([key, value]) => [key, Object.keys(value)])),
      }));
      fs.writeFileSync(output, '[]', 'utf8');
      return;
    }
    const selectedItems = mode === 'match' ? appItems.filter(app => candidates.some(candidate =>
      (candidate.externalId && String(candidate.externalId) === String(app.id)) ||
      (candidate.appid && normalize(candidate.appid) === normalize(app.appid)) ||
      (candidate.miniProgramName && normalize(candidate.miniProgramName) === normalize(app.name))
    )) : appItems.filter(app => newCompanies.includes(String(app.subject || '').trim()));
    const maxSelected = Number(env('VENDOR_MAX_SELECTED', '0'));
    const selected = maxSelected > 0 ? selectedItems.slice(0, maxSelected) : selectedItems;
    const token = await page.evaluate(() => localStorage.getItem('kc_token'));
    if (!token) throw new Error('乙方平台登录令牌不存在');
    const records = [];
    const detailWarnings = [];
    for (const app of selected) {
      const listRecord = normalizeVendorRecord(app, {}, {}, page.url());
      const needsDetail = ['miniProgramName', 'avatarUrl', 'description', 'category']
        .some(field => !listRecord[field]);
      const detailResult = needsDetail ? await page.evaluate(async ({ id, token }) => {
        const headers = { Authorization: `Bearer ${token}` };
        const payloads = [];
        const errors = [];
        for (const url of [`/api/apps/${id}`, `/api/apps/${id}/detail`]) {
          const response = await fetch(url, { headers });
          if (!response.ok) {
            errors.push(`${url}:HTTP ${response.status}`);
            continue;
          }
          try { payloads.push(await response.json()); } catch (_) { errors.push(`${url}:JSON解析失败`); }
        }
        const detail = payloads.reduce((merged, payload) => {
          const value = payload && (payload.app || payload.data || payload.detail || payload.result || payload);
          return value && typeof value === 'object' && !Array.isArray(value) ?
            Object.assign(merged, value) : merged;
        }, {});
        return { detail, warning: payloads.length ? '' : (errors.join('；') || '未返回详情') };
      }, { id: app.id, token }) : { detail: {}, warning: '' };
      const secrets = await page.evaluate(async ({ id, token }) => {
        const response = await fetch(`/api/apps/${id}/secrets`, {
          headers: { Authorization: `Bearer ${token}` },
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json();
      }, { id: app.id, token });
      if (detailResult.warning) detailWarnings.push({ id: app.id, warning: detailResult.warning });
      records.push(normalizeVendorRecord(app, detailResult.detail, secrets, page.url()));
    }
    fs.writeFileSync(output, JSON.stringify(records, null, 2), 'utf8');
    console.log(JSON.stringify({
      mode, count: records.length, newCompanies: newCompanies,
      detailWarnings: detailWarnings.length, detailWarningItems: detailWarnings.slice(0, 20),
    }));
  } catch (error) {
    const stamp = new Date().toISOString().replace(/[:.]/g, '-');
    await page.screenshot({ path: path.join(debugDir, `failure-${stamp}.png`), fullPage: true });
    fs.writeFileSync(path.join(debugDir, `failure-${stamp}.txt`), `URL: ${page.url()}\n\n${error.stack}`, 'utf8');
    throw error;
  } finally {
    await browser.close();
  }
}

if (require.main === module) {
  main().catch(error => {
    console.error(`乙方平台抓取失败：${error.message}`);
    process.exit(1);
  });
}

module.exports = { normalizeVendorRecord, payloadItems, textValue };
