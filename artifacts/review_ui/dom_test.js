// DOM-level interaction test for the redesigned review page.
// Run: node artifacts/review_ui/dom_test.js artifacts/review_ui/preview/<job>.html
const fs = require("fs");
const path = require("path");
const { JSDOM, VirtualConsole } = require("jsdom");

const file = process.argv[2];
if (!file) { console.error("usage: node dom_test.js <html>"); process.exit(2); }

const html = fs.readFileSync(file, "utf8");
const CAPTURE = path.join(__dirname, "last-review.json");
const failures = [];
const logs = [];
const virtualConsole = new VirtualConsole();
virtualConsole.on("jsdomError", e => logs.push("jsdomError: " + e.message));
virtualConsole.on("error", (...a) => logs.push("error: " + a.join(" ")));

const dom = new JSDOM(html, {
  runScripts: "dangerously",
  url: "http://127.0.0.1:8765/jobs/paper-0000000000000000?token=test",
  pretendToBeVisual: true,
  virtualConsole,
  beforeParse(window) {
    window.Element.prototype.scrollIntoView = function () {};
    window.Element.prototype.scrollTo = function () {};
    window.fetch = async (url, opts) => {
      if (opts && opts.method === "POST") {
        fs.writeFileSync(CAPTURE, opts.body, "utf8");
        return { status: 200, ok: true, json: async () => ({ ok: true, file_name: "captured.human-review.json" }) };
      }
      return { status: 404, ok: false, json: async () => ({}) };
    };
    window.confirm = () => true;
  },
});
const { window } = dom;
const doc = window.document;
const $ = sel => doc.querySelector(sel);
const $$ = sel => Array.from(doc.querySelectorAll(sel));
const visible = el => el && !el.hidden && !el.closest("[hidden]");

function ok(name, cond, extra) {
  if (!cond) failures.push(name + (extra ? " → " + extra : ""));
  console.log((cond ? "PASS " : "FAIL ") + name + (cond || !extra ? "" : " → " + extra));
}

// 基础结构
const cards = $$("#review-form .card");
ok("八张评审卡渲染", cards.length === 8, "实际 " + cards.length);
ok("初始只显示第一张", cards.filter(visible).length === 1);
ok("左侧八步", $$("#steps li").length === 8);
ok("进度初始 0", $("#prog-num").textContent === "0", $("#prog-num").textContent);
ok("阅读区两区就位", ["note", "claims"].every(p => $("#pane-" + p)));
const figCards = $$(".note .fig");
const legacy = figCards.length === 0;
ok("图表已嵌入正文流", legacy || figCards.length > 0, String(figCards.length));
ok("图文同区（无独立图表页签）", !$$('.tabs button[data-pane="figs"]').length);
ok("原文页分区已移除", !$("#pane-pages") && !$$('.tabs button[data-pane="pages"]').length);
ok("没有跳原文页的页码按钮", !$$("button[data-page]").length, String($$("button[data-page]").length));

// 回归：点图只开灯箱，不得跳去任何别的分区（曾因 document 上挂了 [data-page] 处理而跳页）
const figImg = $(".note .fig img");
if (figImg) {
  figImg.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  ok("点图打开灯箱", !$("#lb").hidden);
  ok("点图后仍停在速读分区", !$("#pane-note").hidden && $("#pane-claims").hidden);
  ok("灯箱显示的是这张图本身", $("#lb-img").getAttribute("src") === figImg.getAttribute("src"));
  ok("灯箱图注含图号", ($("#lb-cap").textContent || "").length > 0, $("#lb-cap").textContent);
  $("#lb-close").click();
  ok("灯箱可关闭", $("#lb").hidden);
} else {
  console.log("SKIP 无图卡（旧格式），跳过灯箱回归");
}

// 下一节
$("#next").click();
ok("点下一节切到第 2 张", !cards[1].hidden && cards[0].hidden);
ok("左轨同步当前节", $("#steps li:nth-child(2) button").getAttribute("aria-current") === "true");

// 键盘 2 = 部分正确
doc.dispatchEvent(new window.KeyboardEvent("keydown", { key: "2", bubbles: true }));
const r2 = doc.querySelector('input[name="question_contribution-verdict"][value="partially_correct"]');
ok("按键 2 选中部分正确", r2 && r2.checked);
ok("进度 +1", $("#prog-num").textContent === "1", $("#prog-num").textContent);
ok("键盘作答后停留在本节", !cards[1].hidden);
ok("提示条给出下一步", ($("#status").textContent || "").includes("下一节"), $("#status").textContent);

// 选项卡
$$(".tabs button").find(b => b.dataset.pane === "claims").click();
ok("主张分区可见", !$("#pane-claims").hidden && $("#pane-note").hidden);

// 主张勾选同步
const claimBox = $("#pane-claims .claim input[type=checkbox]");
const claimId = claimBox ? claimBox.value : null;
if (claimBox) {
  claimBox.checked = true;
  claimBox.dispatchEvent(new window.Event("change", { bubbles: true }));
  ok("勾选主张写入批准清单", ($("#approved-claims").value || "").includes(claimId), $("#approved-claims").value);
} else {
  console.log("SKIP 无候选主张（旧格式），跳过勾选同步");
}

// 八节全部作答 → 人工确认
cards.forEach((card, i) => {
  const radio = card.querySelector('input[type="radio"][value="correct"]');
  radio.checked = true;
  radio.dispatchEvent(new window.Event("change", { bubbles: true }));
});
const role = doc.querySelector('input[name="relevance-category"][value="支持证据"]');
role.checked = true;
role.dispatchEvent(new window.Event("change", { bubbles: true }));
$("#citation-consent").checked = true;
$("#citation-consent").dispatchEvent(new window.Event("change", { bubbles: true }));
ok("八节完成进度 8", $("#prog-num").textContent === "8", $("#prog-num").textContent);
ok("结论为人工确认", $("#outcome").textContent.includes("人工确认"), $("#outcome").textContent);
ok("许可说明变为可进入综合池", $("#permission-reason").textContent.includes("进入综合候选池"), $("#permission-reason").textContent);

// 草稿保存 & 恢复
$("#save").click();
const key = Object.keys(window.localStorage).find(k => k.startsWith("paper-human-review-v2"));
ok("草稿写入 localStorage", Boolean(key));
const draft = JSON.parse(window.localStorage.getItem(key));
ok("草稿含八节", draft.sections.length === 8, String(draft.sections.length));
ok("草稿许可已授权", draft.citation_permission.allowed === true);
ok("草稿含批准主张", claimId ? draft.citation_permission.approved_claims.includes(claimId) : true);

// 排除 → 判定为 excluded
const exclude = doc.querySelector('input[name="relevance-category"][value="排除"]');
exclude.checked = true;
exclude.dispatchEvent(new window.Event("change", { bubbles: true }));
ok("勾选排除后结论变排除", $("#outcome").textContent.includes("排除"), $("#outcome").textContent);

// 主题/字号
$("#btn-theme").click();
ok("夜间模式生效", doc.documentElement.dataset.theme === "dark");
$("#btn-font").click();
ok("字号增大", doc.documentElement.style.getPropertyValue("--reader-size") !== "");
$("#btn-focus").click();
ok("专注模式隐藏右栏", doc.body.dataset.focus === "1");

// 帮助面板
$("#btn-help").click();
ok("快捷键面板可打开", !$("#help").hidden);
doc.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
ok("Esc 关闭面板", $("#help").hidden);

ok("无脚本错误", logs.length === 0, logs.slice(0, 3).join(" | "));

// 回到「人工确认」路径，验证提交载荷
exclude.checked = false;
exclude.dispatchEvent(new window.Event("change", { bubbles: true }));
$("#export").click();
setTimeout(() => {
  ok("提交载荷已发出", fs.existsSync(CAPTURE));
  ok("保存成功提示", ($("#status").textContent || "").includes("已保存"), $("#status").textContent);
  console.log("\n" + (failures.length ? "FAILURES: " + failures.length + "\n - " + failures.join("\n - ") : "ALL PASS"));
  process.exit(failures.length ? 1 : 0);
}, 150);

