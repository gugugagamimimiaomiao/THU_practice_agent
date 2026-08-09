"use strict";

const state = {
  projects: [],
  stats: {},
  activity: [],
  selectedCompare: new Set(),
  generated: null,
  collector: null,
  collectorEditor: { profileId: null, dirty: false },
  workspace: { projectId: "", siteOptions: [], selectedSiteIds: new Set(), loadedFor: "" },
  page: "dashboard",
};

const pageMeta = {
  dashboard: ["PRACTICE XIAODA", "实践机会总览"],
  projects: ["OPPORTUNITY LIBRARY", "社会实践机会库"],
  ingest: ["SOURCE PIPELINE", "导入与人工审核"],
  recommend: ["PERSONAL MATCH", "智能匹配与推荐"],
  workspace: ["ACTION COPILOT", "实践行动工作台"],
  developer: ["DEVELOPER COLLECTOR", "开发者自动采集"],
};

const statusMeta = {
  published: ["已核验", "published"],
  needs_review: ["待复核", "needs_review"],
  expired: ["已过期", "expired"],
  draft: ["草稿", "needs_review"],
  rejected: ["已拒绝", "expired"],
};

const fieldLabels = {
  signup_deadline: "报名截止",
  practice_dates: "实践日期",
  eligibility: "参与资格",
  reimbursement: "经费报销",
  signup_method: "报名方式",
  source_url: "原文链接",
  location: "实践地点",
  organizer: "主办单位",
};

const kindTitles = {
  application: "报名材料草稿",
  outreach: "当地外联方案",
  interview: "地点适配访谈提纲",
  itinerary: "地点与路线行程任务",
  report: "调研报告框架",
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const type = response.headers.get("content-type") || "";
  const payload = type.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    const message = payload?.error || `请求失败（${response.status}）`;
    throw new Error(payload?.details ? `${message}：${payload.details}` : message);
  }
  return payload;
}

async function developerApi(path, options = {}) {
  const key = $("#developerAdminKey")?.value.trim() || sessionStorage.getItem("practice-xiaoda-admin-key") || "";
  if (key) sessionStorage.setItem("practice-xiaoda-admin-key", key);
  return api(path, { ...options, headers: { ...(options.headers || {}), ...(key ? { Authorization: `Bearer ${key}` } : {}) } });
}

function toast(title, message = "", type = "success") {
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.innerHTML = `<strong>${esc(title)}</strong>${message ? `<p>${esc(message)}</p>` : ""}`;
  $("#toastRegion").appendChild(el);
  setTimeout(() => el.remove(), 3800);
}

function fmtDate(value) {
  if (!value) return "待确认";
  const [y, m, d] = value.slice(0, 10).split("-");
  return `${y}.${m}.${d}`;
}

function fmtTime(value) {
  if (!value) return "";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(d);
}

function locationText(project) {
  const location = project.location || {};
  const mode = { online: "线上", offline: "线下", hybrid: "混合", unknown: "方式待确认" }[location.mode] || "";
  return location.detail || [location.province, location.city, mode].filter(Boolean).join(" · ") || "地点待确认";
}

function reimbursementText(project) {
  const r = project.reimbursement || {};
  if (r.text) return r.text;
  if (r.has_reimbursement === true) return "明确提供报销/补贴";
  if (r.has_reimbursement === false) return "明确不提供报销";
  return "经费待确认";
}

function statusBadge(project) {
  const [label, cls] = statusMeta[project.status] || [project.status, "expired"];
  return `<span class="status-badge ${cls}">${label}</span>${project.demo_data ? '<span class="demo-badge">演示</span>' : ""}`;
}

function goPage(page) {
  if (!pageMeta[page]) return;
  state.page = page;
  $$(".page").forEach((el) => el.classList.toggle("active", el.id === `page-${page}`));
  $$(".nav-item").forEach((el) => el.classList.toggle("active", el.dataset.page === page));
  $("#pageEyebrow").textContent = pageMeta[page][0];
  $("#pageTitle").textContent = pageMeta[page][1];
  $(".sidebar").classList.remove("open");
  window.scrollTo({ top: 0, behavior: "smooth" });
  if (page === "projects") renderProjects();
  if (page === "ingest") renderReviewQueue();
  if (page === "workspace") renderWorkspaceSelect();
  if (page === "developer") loadDeveloperCollector();
}

function setCollectorEditorMeta({ title, hint, dirty = false, credentialConfigured = false, isNew = false }) {
  $("#collectorEditorTitle").textContent = title;
  $("#collectorEditorHint").textContent = hint;
  $("#collectorEditorPill").textContent = dirty || isNew ? "未保存" : "已保存";
  $("#collectorEditorPill").classList.toggle("warn", dirty || isNew);
  $("#credentialState").textContent = credentialConfigured
    ? "此配置已有凭证；留空不会覆盖，重新填写才会替换"
    : "此配置尚未填写凭证；首次运行前需要填写";
}

function fillCollectorForm(data) {
  $("#collectorProfile").value = data.active_profile_id || "__new__";
  $("#collectorProfileName").value = data.profile_name || "默认采集配置";
  $("#collectorEnabled").checked = Boolean(data.enabled);
  $("#collectorDailyTime").value = data.daily_time || "08:15";
  $("#collectorAccounts").value = (data.accounts || []).join(", ");
  $("#collectorPath").value = data.collector_path || "";
  $("#collectorPython").value = data.collector_python || "";
  $("#collectorToken").value = "";
  $("#collectorCookie").value = "";
  state.collectorEditor = { profileId: data.active_profile_id || "__new__", dirty: false };
  setCollectorEditorMeta({
    title: data.profile_name || "默认采集配置",
    hint: "正在编辑已保存的配置。修改后请点击“保存配置”，再点击“更新”运行。",
    credentialConfigured: Boolean(data.credential_configured),
  });
}

function renderDeveloperCollector({ resetForm = false } = {}) {
  const data = state.collector;
  if (!data) return;
  if (resetForm || state.collectorEditor.profileId !== data.active_profile_id) fillCollectorForm(data);
  const rawProgress = data.progress || {};
  const progressPercent = Math.max(0, Math.min(100, Number(rawProgress.percent || 0)));
  const progressLabel = rawProgress.label || (data.running ? "正在采集公众号文章" : data.last_result || "尚未启动采集");
  const progressCount = Number.isInteger(rawProgress.current) && Number.isInteger(rawProgress.total)
    ? `${rawProgress.current} / ${rawProgress.total} 篇候选文章` : "";
  $("#collectorStatusPill").textContent = data.running ? `采集中 ${Math.round(progressPercent)}%` : data.enabled ? "每日更新已启用" : "每日更新未启用";
  $("#collectorStatusPill").classList.toggle("warn", !data.credential_configured || !data.collector_configured);
  $("#developerCollectorStatus").innerHTML = `
    <div class="collector-state"><span>公众号采集器</span><strong>${data.collector_configured ? "已配置" : "待配置"}</strong></div>
    <div class="collector-state"><span>Token / Cookie</span><strong>${data.credential_configured ? "已填写（不回显）" : "待填写"}</strong></div>
    <div class="collector-state"><span>每日任务</span><strong>${data.enabled ? `${esc(data.daily_time)} 自动运行` : "未启用"}</strong></div>
    <div class="collector-state"><span>最近执行</span><strong>${esc(data.last_run_at || "尚未执行")}</strong></div>
    <section class="collector-progress ${data.running ? "running" : ""}" aria-live="polite">
      <div class="collector-progress-head"><strong>${data.running ? "本次采集进度" : "最近采集进度"}</strong><span>${Math.round(progressPercent)}%</span></div>
      <div class="collector-progress-track" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(progressPercent)}"><i style="width:${progressPercent}%"></i></div>
      <p>${esc(progressLabel)}</p>${progressCount ? `<small>${esc(progressCount)}</small>` : ""}
    </section>
    <p class="muted">${esc(data.last_result || "保存设置后可点击“更新”验证。")}</p>`;
  $("#runCollectorBtn").disabled = Boolean(data.running);
  $("#runCollectorBtn").textContent = data.running ? "更新中…" : "更新";
  $("#profileCount").textContent = `${(data.profiles || []).length} 份`;
  $("#savedProfileList").innerHTML = (data.profiles || []).map((profile) => `
    <button type="button" class="saved-profile ${profile.id === data.active_profile_id ? "active" : ""}" data-select-profile="${esc(profile.id)}" aria-pressed="${profile.id === data.active_profile_id}">
      <span class="saved-profile-main"><strong>${esc(profile.name)}</strong><small>${esc((profile.accounts || []).join("、") || "尚未设置公众号")}</small></span>
      <span class="saved-profile-meta"><span>${profile.enabled ? `${esc(profile.daily_time || "08:15")} 每日运行` : "未启用定时"}</span><b>${profile.credential_configured ? "凭证已填" : "待填凭证"}</b></span>
    </button>`).join("") || `<p class="muted">还没有已保存的配置。请点击“新建配置”。</p>`;
}

async function loadDeveloperCollector() {
  try {
    state.collector = await developerApi("/api/developer/collector");
    renderDeveloperCollector({ resetForm: !state.collectorEditor.profileId });
  } catch (error) {
    $("#developerCollectorStatus").innerHTML = `<p class="muted">无法读取开发者设置：${esc(error.message)}</p>`;
  }
}

function developerCollectorPayload() {
  const token = $("#collectorToken").value.trim();
  const cookie = $("#collectorCookie").value.trim();
  return {
    profile_id: $("#collectorProfile").value,
    profile_name: $("#collectorProfileName").value.trim(),
    enabled: $("#collectorEnabled").checked,
    daily_time: $("#collectorDailyTime").value,
    accounts: $("#collectorAccounts").value,
    collector_path: $("#collectorPath").value.trim(),
    collector_python: $("#collectorPython").value.trim(),
    replace_credentials: Boolean(token || cookie),
    token,
    cookie,
  };
}

async function persistDeveloperCollector({ notify = true } = {}) {
  const payload = developerCollectorPayload();
  state.collector = await developerApi("/api/developer/collector", { method: "POST", body: JSON.stringify(payload) });
  renderDeveloperCollector({ resetForm: true });
  if (notify) toast("开发者设置已保存", "Cookie 和 Token 不会在页面中回显。", "success");
}

async function saveDeveloperCollector(event) {
  event.preventDefault();
  if ($("#collectorProfile").value === "__new__" && !$("#collectorProfileName").value.trim()) {
    throw new Error("请先为新配置填写名称，再保存。");
  }
  await persistDeveloperCollector();
}

async function runDeveloperCollector() {
  if ($("#collectorProfile").value === "__new__") throw new Error("请先命名并保存这份新配置，再执行更新。");
  if (state.collectorEditor.dirty) throw new Error("当前配置有未保存修改，请先点击“保存配置”，再更新机会库。");
  const since = $("#collectorBackfillSince").value;
  const count = $("#collectorBackfillCount").value;
  const result = await developerApi("/api/developer/collector/run", { method: "POST", body: JSON.stringify({ since, count }) });
  state.collector = result.status;
  renderDeveloperCollector();
  toast(result.ok ? "每日采集已启动" : "无法启动采集", result.message, result.ok ? "success" : "error");
  if (result.ok) waitForCollectorRefresh();
}

async function waitForCollectorRefresh() {
  setTimeout(async () => {
    try {
      state.collector = await developerApi("/api/developer/collector");
      renderDeveloperCollector();
      if (state.collector.running) {
        waitForCollectorRefresh();
      } else {
        await loadAll();
        toast("机会库已刷新", state.collector.last_result || "采集任务已结束。", "success");
      }
    } catch (_) {
      // A later page refresh remains a safe fallback if the developer panel is closed.
    }
  }, 1000);
}

async function selectDeveloperProfile(profileId) {
  state.collector = await developerApi("/api/developer/collector/select", { method: "POST", body: JSON.stringify({ profile_id: profileId }) });
  renderDeveloperCollector({ resetForm: true });
  toast("已切换采集配置", "已保存的 Token/Cookie 已供当前任务使用，不会回显。", "success");
}

function newDeveloperProfile() {
  $("#collectorProfile").value = "__new__";
  $("#collectorProfileName").value = "";
  $("#collectorEnabled").checked = false;
  $("#collectorDailyTime").value = "08:15";
  $("#collectorAccounts").value = "清华大学社会实践, 无限之声, 清华大学学生公益";
  $("#collectorToken").value = "";
  $("#collectorCookie").value = "";
  state.collectorEditor = { profileId: "__new__", dirty: true };
  setCollectorEditorMeta({ title: "新建采集配置", hint: "填写名称、公众号和凭证后点击“保存配置”。保存前不会影响任何已有配置。", isNew: true });
  $("#collectorProfileName").focus();
  toast("已新建空白配置", "填写后点击“保存配置”即可创建。", "success");
}

async function deleteDeveloperProfile() {
  const profileId = $("#collectorProfile").value;
  if (profileId === "__new__") throw new Error("当前是未保存的新配置，无需删除。");
  const name = state.collector?.profile_name || "当前配置";
  if (!confirm(`确认删除“${name}”吗？删除后不可恢复。`)) return;
  state.collector = await developerApi("/api/developer/collector/delete", { method: "POST", body: JSON.stringify({ profile_id: profileId }) });
  renderDeveloperCollector({ resetForm: true });
  toast("已删除配置", "已切换到保留的另一份配置。", "success");
}

async function loadAll() {
  try {
    const [projectsPayload, statsPayload] = await Promise.all([api("/api/projects"), api("/api/stats")]);
    state.projects = projectsPayload.projects;
    state.stats = statsPayload.stats;
    state.activity = statsPayload.activity;
    renderAll();
  } catch (error) {
    toast("载入失败", error.message, "error");
  }
}

function renderAll() {
  renderStats();
  renderDashboard();
  renderThemeControls();
  renderProjects();
  renderReviewQueue();
  renderWorkspaceSelect();
  $("#navProjectCount").textContent = state.stats.total || 0;
  $("#navReviewCount").textContent = state.stats.needs_review || 0;
}

function renderStats() {
  const s = state.stats;
  $("#statTotal").textContent = s.total ?? "—";
  $("#statPublished").textContent = s.published ?? "—";
  $("#statReview").textContent = s.needs_review ?? "—";
  $("#statSources").textContent = s.sources ?? "—";
  $("#legendPublished").textContent = s.published ?? 0;
  $("#legendReview").textContent = s.needs_review ?? 0;
  $("#legendExpired").textContent = s.expired ?? 0;
  const total = Math.max(1, s.total || 1);
  const p = Math.round((s.published || 0) / total * 100);
  const r = Math.round((s.needs_review || 0) / total * 100);
  $("#healthPercent").textContent = `${p}%`;
  $("#healthDonut").style.background = `conic-gradient(var(--green) 0 ${p}%, var(--amber) ${p}% ${Math.min(100, p + r)}%, #d8d3dc 0)`;
}

function renderDashboard() {
  const featured = [...state.projects]
    .filter((p) => p.status === "published" || p.status === "needs_review")
    .sort((a, b) => Number(a.demo_data) - Number(b.demo_data) || (a.status === "published" ? 1 : -1))
    .slice(0, 4);
  $("#featuredProjects").classList.remove("skeleton-block");
  $("#featuredProjects").innerHTML = featured.length ? featured.map((p, i) => `
    <article class="featured-item" data-open-project="${esc(p.id)}" tabindex="0">
      <i class="featured-bar ${i % 3 === 1 ? "green" : i % 3 === 2 ? "amber" : ""}"></i>
      <div><h4>${esc(p.title)}</h4><div class="featured-meta"><span>⌖ ${esc(locationText(p))}</span><span>⌁ ${(p.theme_tags || []).slice(0,2).map(esc).join(" · ")}</span></div></div>
      <div class="featured-deadline"><strong>${fmtDate(p.signup_deadline)}</strong><span>报名截止</span></div>
    </article>`).join("") : `<p class="muted">暂无已核验项目</p>`;

  $("#activityList").innerHTML = state.activity.length ? state.activity.slice(0, 4).map((a) => `
    <article class="activity-item"><span>${esc(a.event_type)}</span><p>${esc(a.message)}</p><small>${esc(fmtTime(a.created_at))}</small></article>
  `).join("") : `<p class="muted">完成一次导入、推荐或生成后，活动记录会显示在这里。</p>`;
}

function allThemes() {
  return [...new Set(state.projects.flatMap((p) => p.theme_tags || []))].sort((a, b) => a.localeCompare(b, "zh-CN"));
}

function renderThemeControls() {
  const themes = allThemes();
  const select = $("#themeFilter");
  const current = select.value;
  select.innerHTML = `<option value="">全部主题</option>${themes.map((t) => `<option>${esc(t)}</option>`).join("")}`;
  select.value = current;
  $("#themeChecks").innerHTML = themes.map((t) => `<label><input type="checkbox" name="themes" value="${esc(t)}"><span>${esc(t)}</span></label>`).join("");
  restoreProfile();
}

function imageOcrMeta(project) {
  const ocr = project.image_ocr || {};
  const attempted = Number(ocr.attempted || 0);
  const downloaded = Number(ocr.downloaded || 0);
  const processed = Number(ocr.processed || 0);
  const textFound = Number(ocr.text_found || 0);
  if (!project.image_ocr_status) return { badge: "", detail: "" };
  if (project.image_ocr_status === "runtime_unavailable") return { badge: '<span class="image-ocr-badge">OCR 引擎不可用</span>', detail: "当前环境没有 OCR 引擎；下次扫描会自动重试。" };
  if (!attempted) return { badge: '<span class="image-ocr-badge">配图待处理</span>', detail: "已发现原文配图，等待下一次采集处理。" };
  const summary = `下载 ${downloaded}/${attempted} 张 · 已处理 ${processed}/${attempted} 张 · 识别到文字 ${textFound} 张`;
  if (processed === attempted) return { badge: `<span class="image-ocr-badge done">配图已处理 ${processed}/${attempted}</span>`, detail: `已完成全部配图处理（${summary}）。部分图可能是二维码或装饰图，未必含文字。` };
  return { badge: `<span class="image-ocr-badge">配图处理中 ${processed}/${attempted}</span>`, detail: `配图尚未全部完成：${summary}。下次扫描会仅重试下载/OCR失败图片。` };
}

function projectCard(project) {
  const selected = state.selectedCompare.has(project.id);
  const imageOcr = imageOcrMeta(project);
  return `
    <article class="project-card status-${esc(project.status)}" data-project-card="${esc(project.id)}">
      <div class="project-card-head">
        <div class="card-topline"><div>${statusBadge(project)}${imageOcr.badge}</div><label class="compare-check"><input type="checkbox" data-compare="${esc(project.id)}" ${selected ? "checked" : ""}><span>比较</span></label></div>
        <h3>${esc(project.title)}</h3><p class="source-line">${esc(project.source_account || "来源待确认")} · ${esc(project.organizer || "主办方待确认")}</p>
      </div>
      <div class="project-card-body"><p>${esc(project.summary || "暂无摘要")}</p><div class="tag-row">${(project.theme_tags || []).slice(0,4).map((t) => `<span class="tag">${esc(t)}</span>`).join("")}</div>
        <div class="project-facts"><div class="fact"><span>⌖</span><strong>${esc(locationText(project))}</strong></div><div class="fact"><span>◷</span><strong>${fmtDate(project.practice_start)} — ${fmtDate(project.practice_end)}${(project.schedule_segments || []).length > 1 ? "（多时段）" : ""}</strong></div><div class="fact"><span>⌁</span><strong>截止 ${fmtDate(project.signup_deadline)}</strong></div></div>
      </div>
      <div class="project-card-foot"><span class="confidence-badge ${Number(project.confidence) >= .85 ? "high" : ""}">信息置信度 ${Math.round(Number(project.confidence || 0) * 100)}%</span><button class="project-open" data-open-project="${esc(project.id)}">查看证据 →</button></div>
    </article>`;
}

function filteredProjects() {
  const query = $("#projectSearch").value.trim().toLowerCase();
  const status = $("#statusFilter").value;
  const theme = $("#themeFilter").value;
  return state.projects.filter((p) => {
    const hay = JSON.stringify(p).toLowerCase();
    const queryOk = !query || hay.includes(query);
    const statusOk = status === "all" ||
      (status === "active" ? ["published", "needs_review"].includes(p.status) :
        status === "real" ? !p.demo_data : status === "demo" ? Boolean(p.demo_data) : p.status === status);
    const themeOk = !theme || (p.theme_tags || []).includes(theme);
    return queryOk && statusOk && themeOk;
  }).sort((a, b) => Number(a.demo_data) - Number(b.demo_data) || (a.status === "published" ? 1 : -1));
}

function renderProjects() {
  const projects = filteredProjects();
  $("#projectGrid").innerHTML = projects.map(projectCard).join("");
  $("#projectGrid").classList.toggle("hidden", !projects.length);
  $("#projectEmpty").classList.toggle("hidden", Boolean(projects.length));
  const real = projects.filter((p) => !p.demo_data).length;
  $("#projectSummary").textContent = `显示 ${projects.length} / ${state.projects.length} 个项目 · 其中真实采集 ${real} 个`;
  $("#compareCount").textContent = state.selectedCompare.size;
  $("#compareBtn").disabled = state.selectedCompare.size < 2;
}

function evidenceHtml(project) {
  const entries = Object.entries(project.field_evidence || {});
  if (!entries.length) return `<p>尚无字段级证据，项目应保持待复核状态。</p>`;
  return `<div class="evidence-list">${entries.map(([field, item]) => `
    <div class="evidence-item"><strong>${esc(fieldLabels[field] || field)}</strong><q>${esc(item.quote || "")}</q><small>${esc(item.source_location || "原文")} · ${esc({ ocr: "OCR", image_ocr_review: "原图文字复核", text: "正文文本" }[item.extraction_method] || item.extraction_method || "待确认")}</small></div>
  `).join("")}</div>`;
}

function detailModal(project) {
  const eligibility = project.eligibility || {};
  const uncertain = project.uncertain_fields || [];
  const imageOcr = imageOcrMeta(project);
  return `<div class="modal-body">
    <span class="modal-kicker">${esc(project.source_account || "来源待确认")} · ${esc(statusMeta[project.status]?.[0] || project.status)}</span>
    <h2 id="modalTitle">${esc(project.title)}</h2>
    <p class="modal-subtitle">${esc(project.organizer || "主办单位待确认")} ${project.demo_data ? "· 本卡为演示数据" : ""}</p>
    <div class="modal-tag-row">${(project.theme_tags || []).map((t) => `<span class="tag">${esc(t)}</span>`).join("")}</div>
    <div class="detail-grid">
      <div class="detail-block"><span>实践时间</span><strong>${fmtDate(project.practice_start)} — ${fmtDate(project.practice_end)}</strong></div>
      <div class="detail-block"><span>报名截止</span><strong>${fmtDate(project.signup_deadline)}</strong></div>
      <div class="detail-block"><span>地点 / 方式</span><strong>${esc(locationText(project))}</strong></div>
      <div class="detail-block"><span>参与资格</span><strong>${esc(eligibility.restriction_text || "待确认")}</strong></div>
      <div class="detail-block"><span>经费</span><strong>${esc(reimbursementText(project))}</strong></div>
      <div class="detail-block"><span>报名方式</span><strong>${esc(project.signup_method || "待确认")}</strong></div>
    </div>
    ${(project.schedule_segments || []).length ? `<section class="detail-section"><h3>具体可选时段</h3><ul class="schedule-list">${project.schedule_segments.map((segment) => `<li><strong>${esc(segment.label || "活动时段")}</strong>：${fmtDate(segment.start)} — ${fmtDate(segment.end)}${segment.period ? `（每${esc(segment.period)}）` : ""}</li>`).join("")}</ul></section>` : ""}
    ${(project.image_sources || []).length ? `<section class="detail-section"><h3>原文配图</h3><p>${esc(imageOcr.detail)}</p>${(project.image_ocr?.details || []).length ? `<ul class="image-ocr-details">${project.image_ocr.details.map((item) => `<li>配图 ${esc(item.index)}：${esc(item.status === "text_found" ? `已识别 ${item.characters || 0} 字` : item.reason || item.status)}</li>`).join("")}</ul>` : ""}<div class="image-source-links">${project.image_sources.map((url, index) => `<a href="${esc(url)}" target="_blank" rel="noreferrer">查看配图 ${index + 1} ↗</a>`).join("")}</div></section>` : ""}
    <section class="detail-section"><h3>项目摘要</h3><p>${esc(project.summary || "暂无摘要")}</p></section>
    ${uncertain.length ? `<section class="detail-section"><h3>待确认字段</h3><div class="uncertain-list">${uncertain.map((f) => `<span>${esc(fieldLabels[f] || f)}</span>`).join("")}</div></section>` : ""}
    ${(project.risk_notes || []).length ? `<section class="detail-section"><h3>风险提示</h3><p>${project.risk_notes.map(esc).join("；")}</p></section>` : ""}
    <section class="detail-section"><h3>字段证据</h3>${evidenceHtml(project)}</section>
    <section class="review-form">
      <h3>人工复核与修正</h3>
      <form id="modalReviewForm" data-project-id="${esc(project.id)}">
        <div class="form-row two"><label><span>报名截止</span><input name="signup_deadline" type="date" value="${esc(project.signup_deadline || "")}"></label><label><span>审核状态</span><select name="status"><option value="needs_review" ${project.status === "needs_review" ? "selected" : ""}>待复核</option><option value="published" ${project.status === "published" ? "selected" : ""}>已核验，可推荐</option><option value="expired" ${project.status === "expired" ? "selected" : ""}>已过期</option><option value="rejected" ${project.status === "rejected" ? "selected" : ""}>拒绝收录</option></select></label></div>
        <div class="form-row two"><label><span>实践开始</span><input name="practice_start" type="date" value="${esc(project.practice_start || "")}"></label><label><span>实践结束</span><input name="practice_end" type="date" value="${esc(project.practice_end || "")}"></label></div>
        <label><span>原文链接</span><input name="source_url" type="url" value="${esc(project.source_url || "")}" placeholder="https://mp.weixin.qq.com/…"></label>
        <label><span>资格限制原文</span><input name="restriction_text" value="${esc(eligibility.restriction_text || "")}"></label>
        <label><span>经费原文</span><input name="reimbursement_text" value="${esc(project.reimbursement?.text || "")}"></label>
        <label><span>报名方式</span><input name="signup_method" value="${esc(project.signup_method || "")}"></label>
        <div class="detail-actions"><button type="button" class="ghost-button" data-workspace-project="${esc(project.id)}">进入行动工作台</button><button type="submit" class="primary-button">保存复核</button></div>
      </form>
    </section>
  </div>`;
}

function openModal(html) {
  $("#modalContent").innerHTML = html;
  $("#modalBackdrop").classList.remove("hidden");
  document.body.style.overflow = "hidden";
}

function closeModal() {
  $("#modalBackdrop").classList.add("hidden");
  $("#modalContent").innerHTML = "";
  document.body.style.overflow = "";
}

function openProject(id) {
  const project = state.projects.find((p) => p.id === id);
  if (!project) return;
  openModal(detailModal(project));
}

async function saveReview(form) {
  const id = form.dataset.projectId;
  const current = state.projects.find((p) => p.id === id);
  const data = Object.fromEntries(new FormData(form));
  const uncertain = new Set(current.uncertain_fields || []);
  if (data.signup_deadline) uncertain.delete("signup_deadline");
  if (data.source_url) uncertain.delete("source_url");
  if (data.restriction_text) uncertain.delete("eligibility");
  if (data.reimbursement_text) uncertain.delete("reimbursement");
  if (data.signup_method) uncertain.delete("signup_method");
  if (data.practice_start && data.practice_end) uncertain.delete("practice_dates");
  const reimbursementTextValue = data.reimbursement_text.trim();
  const negative = ["不报销", "费用自理", "无报销"].some((x) => reimbursementTextValue.includes(x));
  const patch = {
    signup_deadline: data.signup_deadline || null,
    practice_start: data.practice_start || null,
    practice_end: data.practice_end || null,
    source_url: data.source_url.trim(),
    signup_method: data.signup_method.trim(),
    status: data.status,
    uncertain_fields: [...uncertain],
    confidence: data.status === "published" ? Math.max(.85, Number(current.confidence || 0)) : current.confidence,
    eligibility: { restriction_text: data.restriction_text.trim() },
    reimbursement: { text: reimbursementTextValue, has_reimbursement: reimbursementTextValue ? !negative : null },
  };
  const result = await api(`/api/projects/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify(patch) });
  toast("复核已保存", result.message);
  closeModal();
  await loadAll();
}

function renderReviewQueue() {
  const queue = state.projects.filter((p) => p.status === "needs_review");
  $("#reviewQueue").innerHTML = queue.length ? queue.map((p) => `
    <article class="review-item"><h4>${esc(p.title)}</h4><p>${esc(p.source_account || "未知来源")} · 置信度 ${Math.round(Number(p.confidence || 0) * 100)}%</p><div class="uncertain-list">${(p.uncertain_fields || []).map((f) => `<span>${esc(fieldLabels[f] || f)}</span>`).join("")}</div><div class="review-actions"><button class="review-open" data-open-project="${esc(p.id)}">打开复核</button><button class="review-approve" data-quick-approve="${esc(p.id)}">标记已核验</button></div></article>
  `).join("") : `<div class="empty-state"><div>✓</div><h3>审核队列已清空</h3><p>新的高风险项目会自动进入这里。</p></div>`;
}

async function quickApprove(id) {
  const project = state.projects.find((p) => p.id === id);
  if (!project) return;
  if ((project.uncertain_fields || []).length) {
    toast("仍有字段待确认", "请先打开项目，补充证据或明确保留风险。", "error");
    openProject(id);
    return;
  }
  await api(`/api/projects/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify({ status: "published", confidence: Math.max(.85, Number(project.confidence || 0)) }) });
  toast("已加入正式推荐池");
  await loadAll();
}

function updateCompare(id, checked) {
  if (checked) {
    if (state.selectedCompare.size >= 3) {
      toast("最多比较 3 个项目", "请先取消一个已选项目。", "error");
      const input = $(`[data-compare="${CSS.escape(id)}"]`);
      if (input) input.checked = false;
      return;
    }
    state.selectedCompare.add(id);
  } else state.selectedCompare.delete(id);
  renderProjects();
}

function compareModal() {
  const projects = [...state.selectedCompare].map((id) => state.projects.find((p) => p.id === id)).filter(Boolean);
  const rows = [
    ["状态", (p) => statusMeta[p.status]?.[0] || p.status],
    ["报名截止", (p) => fmtDate(p.signup_deadline)],
    ["实践时间", (p) => `${fmtDate(p.practice_start)} — ${fmtDate(p.practice_end)}`],
    ["地点", locationText],
    ["主题", (p) => (p.theme_tags || []).join("、")],
    ["参与资格", (p) => p.eligibility?.restriction_text || "待确认"],
    ["经费", reimbursementText],
    ["待确认", (p) => (p.uncertain_fields || []).map((f) => fieldLabels[f] || f).join("、") || "无"],
  ];
  openModal(`<div class="modal-body"><span class="modal-kicker">SIDE BY SIDE</span><h2 id="modalTitle">项目比较</h2><p class="modal-subtitle">比较不会覆盖资格硬过滤；报名之前仍需打开原文核验。</p><div style="overflow:auto"><table class="compare-table"><thead><tr><th>维度</th>${projects.map((p) => `<th>${esc(p.title)}</th>`).join("")}</tr></thead><tbody>${rows.map(([label, fn]) => `<tr><th>${label}</th>${projects.map((p) => `<td>${esc(fn(p))}</td>`).join("")}</tr>`).join("")}</tbody></table></div></div>`);
}

const sampleNotice = `滇南乡村儿童阅读与数字素养实践招募（演示通知）
主办单位：清华大学某学生实践团队
实践时间：2026年8月14日—2026年8月20日
实践地点：云南省红河州（线下）
招募对象：面向全校本科生、研究生，无专业限制
报名截止：2026年7月30日 20:00
经费说明：提供每人最高1800元交通与住宿补贴
报名方式：扫描原文二维码填写报名表
所需材料：报名表、个人陈述，后续安排面试
联系人：演示联系人（请勿外联）
项目将通过教师访谈、学生工作坊与观察记录，了解乡村学校儿童阅读和数字资源使用情况，形成调研报告与课程建议。`;

function setIngestMode(type) {
  const isLink = type === "wechat_url";
  $("#inputType").value = isLink ? "wechat_url" : "copied_text";
  $$(".source-tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.type === type));
  $$(".link-import-fields").forEach((el) => el.classList.toggle("hidden", !isLink));
  $$(".manual-import-fields").forEach((el) => el.classList.toggle("hidden", isLink));
  $("#ingestSubmitBtn").innerHTML = isLink ? '导入公众号链接 <span>→</span>' : '导入手动信息 <span>→</span>';
}

async function submitIngest(event) {
  event.preventDefault();
  const isLink = $("#inputType").value === "wechat_url";
  const payload = {
    input_type: $("#inputType").value,
    source_account: $("#sourceAccount").value.trim(),
    title: $("#sourceTitle").value.trim(),
    source_url: isLink ? $("#sourceUrl").value.trim() : $("#manualSourceUrl").value.trim(),
    raw_text: $("#rawText").value.trim(),
    wechat_cookie: isLink ? $("#userWechatCookie").value.trim() : "",
  };
  const button = event.submitter;
  if (button) { button.disabled = true; button.textContent = "正在提取…"; }
  try {
    const result = await api("/api/ingest", { method: "POST", body: JSON.stringify(payload) });
    const box = $("#ingestResult");
    box.classList.remove("hidden", "warning");
    if (result.status === "needs_text" || result.status === "fetch_failed") {
      box.classList.add("warning");
      box.innerHTML = `<h4>${result.status === "fetch_failed" ? "暂未读取到公众号正文" : "已保存为待补全文线索"}</h4><p>${esc(result.action_required)}</p><p>${esc(result.truthfulness_note)}</p>`;
      toast("线索已保存", "请稍后重试，或补充正文 / OCR 文本。", "success");
    } else {
      const p = result.project;
      box.innerHTML = `<h4>${result.merged_duplicate ? "已合并到已有项目" : "项目卡提取完成"}</h4><p><strong>${esc(p.title)}</strong><br>状态：${esc(statusMeta[p.status]?.[0] || p.status)} · 置信度 ${Math.round(Number(p.confidence || 0) * 100)}% · 待确认 ${(p.uncertain_fields || []).length} 项</p><div class="result-actions"><button class="secondary-button" data-open-project="${esc(p.id)}">查看项目卡</button><button class="primary-button" data-go="projects">进入机会库</button></div>`;
      toast("导入成功", result.review_required ? "项目已进入人工复核队列。" : "项目已通过结构校验。", "success");
      await loadAll();
    }
  } catch (error) {
    toast("导入失败", error.message, "error");
  } finally {
    $("#userWechatCookie").value = "";
    if (button) { button.disabled = false; button.innerHTML = '提取项目卡 <span>→</span>'; }
  }
}

function collectProfile() {
  return {
    department: $("#profileDepartment").value.trim(),
    grade: $("#profileGrade").value,
    available_start: $("#availableStart").value,
    available_end: $("#availableEnd").value,
    themes: $$('input[name="themes"]:checked').map((el) => el.value),
    preferred_locations: $$('input[name="locations"]:checked').map((el) => el.value),
    reimbursement_preference: $("#reimbursementPreference").value,
  };
}

function saveProfile(profile) {
  localStorage.setItem("practice-xiaoda-profile", JSON.stringify(profile));
  $("#profileSaved").textContent = "已在本机保存";
}

// 默认可用时间必须跟着"今天"走。写死日期会随时间推移把所有项目判成时间冲突，
// 首次打开推荐页就只剩零星几条结果，看起来像匹配功能坏了。
function defaultAvailability() {
  const iso = (offsetDays) => {
    const d = new Date();
    d.setDate(d.getDate() + offsetDays);
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  };
  return { start: iso(0), end: iso(60) };
}

function applyDefaultAvailability() {
  const { start, end } = defaultAvailability();
  if (!$("#availableStart").value) $("#availableStart").value = start;
  if (!$("#availableEnd").value) $("#availableEnd").value = end;
}

function restoreProfile() {
  let profile = null;
  try { profile = JSON.parse(localStorage.getItem("practice-xiaoda-profile")); } catch (_) {}
  if (!profile) { applyDefaultAvailability(); return; }
  const fallback = defaultAvailability();
  // 本机存过的可用时间可能已经整段过期（换了学期、或存的是旧版本的默认值）。
  // 这种情况下继续沿用会把所有项目判成时间冲突，所以退回到以今天为准的默认值。
  const stale = !profile.available_end || profile.available_end < fallback.start;
  $("#profileDepartment").value = profile.department || "";
  $("#profileGrade").value = profile.grade || "";
  $("#availableStart").value = stale ? fallback.start : (profile.available_start || fallback.start);
  $("#availableEnd").value = stale ? fallback.end : profile.available_end;
  $("#reimbursementPreference").value = profile.reimbursement_preference || "not_important";
  $$('input[name="themes"]').forEach((el) => { el.checked = (profile.themes || []).includes(el.value); });
  $$('input[name="locations"]').forEach((el) => { el.checked = (profile.preferred_locations || []).includes(el.value); });
}

function matchCard(item, type) {
  const p = item.project;
  const reasons = (item.reasons || []).slice(0, 2);
  const warnings = (type === "excluded" ? item.excluded_reasons : item.warnings || []).slice(0, 2);
  return `<article class="match-card ${type}">
    <div class="match-score" style="--score:${Math.round(item.score)}%"><strong>${Math.round(item.score)}</strong></div>
    <div class="match-main"><h4>${esc(p.title)}</h4><p>${esc(locationText(p))} · 截止 ${fmtDate(p.signup_deadline)}</p>${reasons.map((r) => `<p class="match-reason">✓ ${esc(r)}</p>`).join("")}${warnings.map((w) => `<p class="match-warning">! ${esc(w)}</p>`).join("")}</div>
    <div class="match-actions"><button class="secondary-button small" data-open-project="${esc(p.id)}">查看证据</button>${type !== "excluded" ? `<button class="primary-button" data-workspace-project="${esc(p.id)}">开始准备</button>` : ""}</div>
  </article>`;
}

function renderRecommendation(result) {
  const sections = [];
  if (result.eligible.length) sections.push(`<section class="result-section"><div class="result-section-head"><h3>正式推荐</h3><span>${result.eligible.length} 个已核验项目</span></div>${result.eligible.map((x) => matchCard(x, "eligible")).join("")}</section>`);
  if (result.potential.length) sections.push(`<section class="result-section"><div class="result-section-head"><h3>潜在机会</h3><span>需要先确认关键信息</span></div>${result.potential.map((x) => matchCard(x, "potential")).join("")}</section>`);
  if (result.excluded.length) sections.push(`<details class="result-section"><summary class="result-section-head"><h3>已排除项目</h3><span>${result.excluded.length} 个，点击查看原因</span></summary>${result.excluded.map((x) => matchCard(x, "excluded")).join("")}</details>`);
  $("#recommendResults").innerHTML = sections.join("") || `<div class="recommend-placeholder"><h3>没有可推荐项目</h3><p>请调整硬条件，或导入更多项目。</p></div>`;
}

async function submitRecommend(event) {
  event.preventDefault();
  const profile = collectProfile();
  if (profile.available_start && profile.available_end && profile.available_start > profile.available_end) {
    toast("日期范围无效", "开始日期不能晚于结束日期。", "error"); return;
  }
  saveProfile(profile);
  $("#recommendResults").innerHTML = `<div class="recommend-placeholder"><div class="radar"><span>◎</span></div><h3>正在匹配项目…</h3></div>`;
  try {
    const result = await api("/api/recommend", { method: "POST", body: JSON.stringify({ profile }) });
    renderRecommendation(result);
    toast("推荐已生成", `${result.eligible.length} 个正式匹配，${result.potential.length} 个潜在机会。`);
  } catch (error) {
    toast("推荐失败", error.message, "error");
  }
}

function renderWorkspaceSelect(selectedId = "") {
  const select = $("#workspaceProject");
  const current = selectedId || select.value;
  const projects = state.projects.filter((p) => p.status !== "expired" && p.status !== "rejected");
  select.innerHTML = `<option value="">请选择项目</option>${projects.map((p) => `<option value="${esc(p.id)}">${p.status === "needs_review" ? "[待复核] " : ""}${esc(p.title)}</option>`).join("")}`;
  if (projects.some((p) => p.id === current)) select.value = current;
  if (select.value && select.value !== state.workspace.projectId) resetWorkspaceSites(select.value);
}

function goWorkspace(id) {
  goPage("workspace");
  renderWorkspaceSelect(id);
  $("#workspaceProject").value = id;
  resetWorkspaceSites(id);
  const project = state.projects.find((p) => p.id === id);
  toast("已载入项目", project?.title || "");
}

function workspaceSiteContext() {
  return {
    local_info: $("#contextLocalInfo").value.trim(),
  };
}

function selectedSites() {
  return state.workspace.siteOptions.filter((site) => state.workspace.selectedSiteIds.has(site.id));
}

function renderLocalSites() {
  const panel = $("#localSitePanel");
  const options = state.workspace.siteOptions;
  const count = selectedSites().length;
  panel.classList.toggle("hidden", !options.length);
  $("#selectedSiteCount").textContent = count ? `已选 ${count} 个` : "未选择";
  $("#localSiteHint").textContent = options.length
    ? `推荐范围：${state.workspace.area || "项目地"}。这些是待核验线索，勾选不代表已预约；联系方式只通过官网、官方公众号或公开名录获取。`
    : "选择项目后可生成推荐地点。";
  $("#localSiteOptions").innerHTML = options.map((site) => `
    <label class="local-site-option">
      <input type="checkbox" data-local-site="${esc(site.id)}" ${state.workspace.selectedSiteIds.has(site.id) ? "checked" : ""}>
      <span class="site-check"></span>
      <span class="site-copy"><strong>${esc(site.name)}</strong><small>${esc(site.category)} · ${esc(site.value)}</small><em>访谈主题：${esc((site.interview_topics || []).join("、"))}</em></span>
    </label>`).join("");
}

async function loadLocalSites({ preserveSelection = true } = {}) {
  const projectId = $("#workspaceProject").value;
  if (!projectId) return;
  const selectedNames = preserveSelection ? new Set(selectedSites().map((site) => site.name)) : new Set();
  $("#localSiteHint").textContent = "正在按项目地点和实践主题生成可核验的外联线索…";
  try {
    const data = await api("/api/workspace/local-sites", { method: "POST", body: JSON.stringify({ project_id: projectId, context: workspaceSiteContext() }) });
    state.workspace = {
      projectId,
      loadedFor: projectId,
      area: data.area,
      siteOptions: data.options || [],
      selectedSiteIds: new Set((data.options || []).filter((site) => selectedNames.has(site.name)).map((site) => site.id)),
    };
    renderLocalSites();
  } catch (error) {
    toast("地点推荐加载失败", error.message, "error");
  }
}

function resetWorkspaceSites(projectId) {
  state.workspace = { projectId: projectId || "", siteOptions: [], selectedSiteIds: new Set(), loadedFor: "" };
  if (projectId) loadLocalSites({ preserveSelection: false });
  else renderLocalSites();
}

function renderWorkspaceContext(kind = $("#generatorKind").value) {
  const application = kind === "application";
  const placeBased = ["outreach", "interview", "itinerary"].includes(kind);
  const outreach = kind === "outreach";
  $("#contextOutreachFields").classList.toggle("hidden", !outreach);
  $("#contextApplicationFields").classList.toggle("hidden", !application);
  $("#contextInterviewFields").classList.toggle("hidden", kind !== "interview");
  $("#contextRouteFields").classList.toggle("hidden", kind !== "itinerary");
  $("#localSitePanel").classList.toggle("hidden", !placeBased || !state.workspace.siteOptions.length);
  $("#contextReportFields").classList.toggle("hidden", kind !== "report");
  if (placeBased && $("#workspaceProject").value && !state.workspace.siteOptions.length) loadLocalSites({ preserveSelection: false });
}

function routeEvidenceText(result) {
  if (!result.routes?.length) return result.message || "地图未返回可用路线。";
  const lines = ["实时地图查询结果（生成时间以地图服务返回为准）："];
  for (const route of result.routes) {
    const transit = route.transit ? `公共交通 ${route.transit.minutes ?? "?"} 分钟${route.transit.distance_km != null ? ` / ${route.transit.distance_km} km` : ""}${route.transit.lines?.length ? ` / ${route.transit.lines.join("、")}` : ""}` : "公共交通：未返回";
    const driving = route.driving ? `驾车 ${route.driving.minutes ?? "?"} 分钟${route.driving.distance_km != null ? ` / ${route.driving.distance_km} km` : ""}` : "驾车：未返回";
    const walking = route.walking ? `步行 ${route.walking.minutes ?? "?"} 分钟${route.walking.distance_km != null ? ` / ${route.walking.distance_km} km` : ""}` : "步行：未返回";
    lines.push(`- ${route.site}（解析为：${route.resolved_address}）：${transit}；${driving}；${walking}。建议：${route.recommendation}`);
  }
  return lines.join("\n");
}

async function generateDraft() {
  const projectId = $("#workspaceProject").value;
  if (!projectId) { toast("请先选择项目", "行动草稿必须绑定一个具体项目。", "error"); return; }
  const kind = $("#generatorKind").value;
  if (["interview", "itinerary"].includes(kind) && !selectedSites().length && !$("#contextPlace").value.trim()) {
    toast("请先选择当地外联地点", "在“当地外联”中勾选点位后，才能生成地点适配的访谈或路线方案。", "error"); return;
  }
  if (kind === "itinerary" && !$("#contextHotel").value.trim()) {
    toast("请填写酒店位置", "酒店名称或详细地址是路线任务的必要输入。", "error"); return;
  }
  const button = $("#generateBtn");
  button.disabled = true; button.textContent = "正在生成…";
  try {
    const result = await api("/api/generate", {
      method: "POST",
      body: JSON.stringify({
        project_id: projectId,
        kind: $("#generatorKind").value,
        context: {
          name: $("#contextName").value.trim(),
          department: $("#contextDepartment").value.trim(),
          strengths: $("#contextStrengths").value.trim(),
          motivation: $("#contextMotivation").value.trim(),
          ideal_role: $("#contextIdealRole").value.trim(),
          contribution: $("#contextContribution").value.trim(),
          goal: $("#contextGoal").value.trim() || $("#contextMotivation").value.trim(),
          local_info: $("#contextLocalInfo").value.trim(),
          place: $("#contextPlace").value.trim(),
          selected_sites: selectedSites(),
          hotel: $("#contextHotel").value.trim(),
          departure: $("#contextDeparture").value.trim(),
          route_evidence: $("#contextRouteEvidence").value.trim(),
          practice_gains: $("#contextPracticeGains").value.trim(),
        },
      }),
    });
    state.generated = result;
    $("#generatedOutput").textContent = result.content;
    $("#outputTitle").textContent = kindTitles[result.kind] || "行动草稿";
    $("#copyOutput").disabled = false;
    $("#downloadOutput").disabled = false;
    $("#feedbackStrip").classList.remove("hidden");
    const warningBox = $("#outputWarnings");
    if (result.warnings.length) { warningBox.textContent = result.warnings.join("；"); warningBox.classList.remove("hidden"); }
    else warningBox.classList.add("hidden");
    toast("草稿已生成", "请核实事实后再对外使用。", "success");
  } catch (error) {
    toast("生成失败", error.message, "error");
  } finally {
    button.disabled = false; button.innerHTML = '生成行动草稿 <span>✦</span>';
  }
}

async function sendFeedback(rating) {
  if (!state.generated) return;
  await api("/api/feedback", { method: "POST", body: JSON.stringify({ project_id: $("#workspaceProject").value, rating, outcome: state.generated.kind, comment: "工作台快速反馈" }) });
  $("#feedbackStrip").innerHTML = `<span>谢谢，你的反馈已记录。</span>`;
  toast("反馈已记录");
}

function downloadText(filename, content, type = "text/markdown;charset=utf-8") {
  const blob = new Blob([content], { type });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = filename; a.click();
  URL.revokeObjectURL(url);
}

document.addEventListener("click", async (event) => {
  const nav = event.target.closest("[data-page]");
  const go = event.target.closest("[data-go]");
  const open = event.target.closest("[data-open-project]");
  const workspace = event.target.closest("[data-workspace-project]");
  const quick = event.target.closest("[data-quick-approve]");
  const rating = event.target.closest("[data-rating]");
  if (nav) goPage(nav.dataset.page);
  if (go) { closeModal(); goPage(go.dataset.go); }
  if (open) openProject(open.dataset.openProject);
  if (workspace) { closeModal(); goWorkspace(workspace.dataset.workspaceProject); }
  if (quick) { try { await quickApprove(quick.dataset.quickApprove); } catch (error) { toast("审核失败", error.message, "error"); } }
  if (rating) { try { await sendFeedback(Number(rating.dataset.rating)); } catch (error) { toast("反馈失败", error.message, "error"); } }
});

document.addEventListener("change", (event) => {
  if (event.target.matches("[data-compare]")) updateCompare(event.target.dataset.compare, event.target.checked);
  if (event.target.matches("[data-local-site]")) {
    const id = event.target.dataset.localSite;
    if (event.target.checked) state.workspace.selectedSiteIds.add(id);
    else state.workspace.selectedSiteIds.delete(id);
    renderLocalSites();
  }
  if (event.target.id === "workspaceProject") resetWorkspaceSites(event.target.value);
});

document.addEventListener("submit", async (event) => {
  if (event.target.id === "modalReviewForm") {
    event.preventDefault();
    try { await saveReview(event.target); } catch (error) { toast("保存失败", error.message, "error"); }
  }
});

window.addEventListener("DOMContentLoaded", () => {
  $$("[data-page]").forEach((el) => el.addEventListener("click", () => goPage(el.dataset.page)));
  $("#mobileMenu").addEventListener("click", () => $(".sidebar").classList.toggle("open"));
  $("#modalClose").addEventListener("click", closeModal);
  $("#modalBackdrop").addEventListener("click", (e) => { if (e.target === e.currentTarget) closeModal(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });
  $("#projectSearch").addEventListener("input", renderProjects);
  $("#statusFilter").addEventListener("change", renderProjects);
  $("#themeFilter").addEventListener("change", renderProjects);
  $("#compareBtn").addEventListener("click", compareModal);
  $("#ingestForm").addEventListener("submit", submitIngest);
  $("#developerCollectorForm").addEventListener("submit", async (event) => { try { await saveDeveloperCollector(event); } catch (error) { toast("保存失败", error.message, "error"); } });
  $$("#developerCollectorForm input, #developerCollectorForm textarea").forEach((field) => field.addEventListener("input", () => {
    if (["developerAdminKey", "collectorBackfillSince", "collectorBackfillCount"].includes(field.id)) return;
    state.collectorEditor.dirty = true;
    const isNew = $("#collectorProfile").value === "__new__";
    setCollectorEditorMeta({
      title: isNew ? "新建采集配置" : $("#collectorProfileName").value.trim() || "未命名配置",
      hint: isNew ? "填写名称、公众号和凭证后点击“保存配置”。" : "修改尚未保存。保存后才会影响下一次“更新”。",
      dirty: true,
      credentialConfigured: Boolean(state.collector?.credential_configured),
      isNew,
    });
  }));
  $("#newProfileBtn").addEventListener("click", () => newDeveloperProfile());
  $("#deleteProfileBtn").addEventListener("click", async () => { try { await deleteDeveloperProfile(); } catch (error) { toast("删除失败", error.message, "error"); } });
  $("#savedProfileList").addEventListener("click", async (event) => {
    const item = event.target.closest("[data-select-profile]");
    if (!item) return;
    const profileId = item.dataset.selectProfile;
    if (profileId === $("#collectorProfile").value) return;
    if (state.collectorEditor.dirty && !confirm("当前修改尚未保存。切换后这些修改会丢失，仍要切换吗？")) return;
    try { await selectDeveloperProfile(profileId); } catch (error) { toast("切换失败", error.message, "error"); }
  });
  $("#runCollectorBtn").addEventListener("click", async () => { try { await runDeveloperCollector(); } catch (error) { toast("启动失败", error.message, "error"); } });
  $("#fillSampleBtn").addEventListener("click", () => { setIngestMode("copied_text"); $("#rawText").value = sampleNotice; $("#manualSourceUrl").value = "https://example.invalid/demo-import"; $("#sourceTitle").value = ""; });
  $$(".source-tab").forEach((tab) => tab.addEventListener("click", () => {
    setIngestMode(tab.dataset.type);
  }));
  $("#recommendForm").addEventListener("submit", submitRecommend);
  $("#refreshSiteOptions").addEventListener("click", () => loadLocalSites());
  $("#queryLiveTransport").addEventListener("click", async () => {
    const hotel = $("#contextHotel").value.trim();
    const sites = selectedSites();
    if (!hotel || !sites.length) { toast("请先填写酒店并选择地点", "实时交通查询需要起点酒店和至少一个目的地。", "error"); return; }
    const button = $("#queryLiveTransport");
    button.disabled = true; button.textContent = "查询中…";
    $("#routeQueryStatus").textContent = "正在查询公共交通、驾车与步行方案…";
    try {
      const result = await api("/api/workspace/transport", { method: "POST", body: JSON.stringify({ project_id: $("#workspaceProject").value, hotel, selected_sites: sites }) });
      $("#routeQueryStatus").textContent = result.message || "交通查询完成。";
      if (result.routes?.length) {
        $("#contextRouteEvidence").value = routeEvidenceText(result);
        toast("实时交通已写入", `已查询 ${result.routes.length} 个地点的交通选项。`, "success");
      } else toast("暂未获得自动路线", result.message || "可改用地图核验。", "error");
    } catch (error) {
      $("#routeQueryStatus").textContent = `查询失败：${error.message}`;
      toast("交通查询失败", error.message, "error");
    } finally {
      button.disabled = false; button.textContent = "查询实时交通";
    }
  });
  $("#openMapSearch").addEventListener("click", () => {
    const hotel = $("#contextHotel").value.trim();
    const firstSite = selectedSites()[0]?.name || $("#contextPlace").value.trim().split(/[\n，、]/)[0];
    if (!hotel || !firstSite) { toast("请先填写酒店并选择地点", "地图核验需要起点酒店和至少一个目的地。", "error"); return; }
    const query = `${hotel} 到 ${firstSite} 公交 地铁 驾车 步行`;
    window.open(`https://www.amap.com/search?query=${encodeURIComponent(query)}`, "_blank", "noopener");
    toast("已打开地图搜索", "核验后请把公共交通、驾车和步行时间粘贴回路线输入框。", "success");
  });
  $$(".generator-tab").forEach((tab) => tab.addEventListener("click", () => {
    $$(".generator-tab").forEach((x) => x.classList.toggle("active", x === tab));
    $("#generatorKind").value = tab.dataset.kind;
    $("#outputTitle").textContent = kindTitles[tab.dataset.kind];
    renderWorkspaceContext(tab.dataset.kind);
  }));
  $("#generateBtn").addEventListener("click", generateDraft);
  $("#copyOutput").addEventListener("click", async () => {
    if (!state.generated) return;
    try { await navigator.clipboard.writeText(state.generated.content); toast("已复制到剪贴板"); }
    catch (_) { toast("复制失败", "请手动选择文本复制。", "error"); }
  });
  $("#downloadOutput").addEventListener("click", () => {
    if (state.generated) downloadText(`实践小搭-${state.generated.kind}.md`, state.generated.content);
  });
  $("#exportBtn").addEventListener("click", () => { window.location.href = "/api/export"; });
  renderWorkspaceContext();
  loadAll();
});
