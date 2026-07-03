const app = document.getElementById("admin-app");
const TAB_SESSION_MODE_KEY = "mnemosyne:tab-session-mode";
const TAB_SESSION_TOKEN_KEY = "mnemosyne:tab-session-token";

let state = {
  user: null,
  users: [],
  personas: [],
  selectedUserId: null,
  selectedPersonaId: null,
  review: null,
  traces: [],
  expressionUsage: null,
  expressionAssets: [],
  revisions: [],
  growth: null,
  versions: [],
  evalRuns: [],
  llmCalls: [],
  llmRoutes: null,
  growthDemo: null,
  expressionAssetFilter: "all",
  runningEval: false,
  generatingRevision: false,
  autoReviewingRevisions: false,
  cleaningStaleRevisions: false,
  revisionNotice: "",
  editingInsight: false,
  error: "",
};

function tabSessionMode() {
  try {
    return sessionStorage.getItem(TAB_SESSION_MODE_KEY) === "1";
  } catch {
    return false;
  }
}

function tabSessionToken() {
  try {
    return sessionStorage.getItem(TAB_SESSION_TOKEN_KEY) || "";
  } catch {
    return "";
  }
}

async function api(path, options = {}) {
  const isolated = tabSessionMode();
  const token = tabSessionToken();
  const isFormData = options.body instanceof FormData;
  const headers = { ...(isFormData ? {} : { "Content-Type": "application/json" }), ...(options.headers || {}) };
  if (isolated && token) headers.Authorization = `Bearer ${token}`;
  const res = await fetch(path, {
    ...options,
    credentials: isolated ? "omit" : "same-origin",
    headers,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
  return data;
}

function h(tag, attrs = {}, children = []) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") el.className = value;
    else if (key === "text") el.textContent = value;
    else if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2), value);
    else if (value !== undefined && value !== null) el.setAttribute(key, value);
  }
  for (const child of Array.isArray(children) ? children : [children]) {
    if (child === null || child === undefined) continue;
    el.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return el;
}

async function bootstrap() {
  try {
    const me = await api("/api/me");
    if (me.user.role !== "admin") throw new Error("当前账号不是管理员。");
    state.user = me.user;
    const users = await api("/api/admin/users");
    state.users = users.users;
    state.selectedUserId = state.users[0]?.id || me.user.id;
    await loadPersonas();
    render();
  } catch (err) {
    app.className = "admin-auth";
    app.replaceChildren(
      h("section", { class: "auth-card" }, [
        h("p", { class: "eyebrow", text: "忆界树 / Project Mnemosyne" }),
        h("h1", { text: "管理台不可用" }),
        h("p", { class: "muted", text: err.message }),
        h("button", { type: "button", text: "回到聊天", onclick: () => { window.location.href = "/"; } }),
      ])
    );
  }
}

async function loadPersonas() {
  const previousPersonaId = state.selectedPersonaId;
  const [users, data] = await Promise.all([
    api("/api/admin/users"),
    api(`/api/admin/personas?target_user_id=${state.selectedUserId}`),
  ]);
  state.users = users.users;
  state.personas = data.personas;
  state.selectedPersonaId = state.personas.some((persona) => Number(persona.id) === Number(previousPersonaId))
    ? previousPersonaId
    : state.personas[0]?.id || null;
  await loadReview();
}

async function loadReview() {
  if (!state.selectedUserId || !state.selectedPersonaId) {
    state.review = null;
    state.traces = [];
    state.expressionUsage = null;
    state.expressionAssets = [];
    state.revisions = [];
    state.growth = null;
    state.versions = [];
    state.llmRoutes = null;
    return;
  }
  const [data, traceData, expressionData, assetData, revisionData, growthData, versionData, evalData, llmData, routeData] = await Promise.all([
    api(`/api/admin/memory/review?target_user_id=${state.selectedUserId}&persona_id=${state.selectedPersonaId}&include_history=true`),
    api(`/api/admin/chat-context-traces?target_user_id=${state.selectedUserId}&persona_id=${state.selectedPersonaId}&limit=6`),
    api(`/api/admin/expression-usage?target_user_id=${state.selectedUserId}&persona_id=${state.selectedPersonaId}&limit=12&usage_limit=80`),
    api("/api/admin/expression-assets"),
    api(`/api/admin/persona-revisions?target_user_id=${state.selectedUserId}&persona_id=${state.selectedPersonaId}&limit=8`),
    api(`/api/admin/persona-growth?target_user_id=${state.selectedUserId}&persona_id=${state.selectedPersonaId}`),
    api(`/api/admin/persona-versions?target_user_id=${state.selectedUserId}&persona_id=${state.selectedPersonaId}&limit=10`),
    api("/api/admin/evaluations/memory/runs?limit=5"),
    api("/api/admin/llm-calls?limit=20"),
    api("/api/admin/llm-routes"),
  ]);
  state.review = data.review;
  state.traces = traceData.traces;
  state.expressionUsage = expressionData;
  state.expressionAssets = assetData.assets || [];
  state.revisions = revisionData.suggestions;
  state.growth = growthData;
  state.versions = versionData.versions;
  state.evalRuns = evalData.runs;
  state.llmCalls = llmData.calls;
  state.llmRoutes = routeData;
}

function render() {
  app.className = "admin-shell";
  app.replaceChildren(renderSidebar(), renderMain());
}

function renderSidebar() {
  return h("aside", { class: "sidebar" }, [
    h("div", { class: "brand" }, [
      h("p", { class: "eyebrow", text: "Project Mnemosyne / Admin" }),
      h("h1", { text: "忆界树" }),
      h("small", { text: state.user?.username || "" }),
    ]),
    h("section", {}, [
      h("div", { class: "section-head" }, [h("strong", { text: "用户" })]),
      h("div", { class: "user-list" }, state.users.map(renderUserButton)),
    ]),
    h("button", { type: "button", class: "ghost full", text: "回到聊天", onclick: () => { window.location.href = "/"; } }),
  ]);
}

function renderUserButton(user) {
  const label = user.nickname || user.username;
  const pending = Number(user.pending_revision_count || 0);
  const requests = Number(user.pending_preference_request_count || 0);
  const adjustment = Number(user.adjustment_feedback_count || 0);
  const stale = Number(user.stale_revision_count || 0);
  const queueText = [
    pending ? `待审 ${pending}` : "",
    requests ? `主动请求 ${requests}` : "",
    adjustment ? `待跟进 ${adjustment}` : "",
    stale ? `过期 ${stale}` : "",
  ].filter(Boolean).join(" / ");
  return h("button", {
    type: "button",
    class: `list-item ${Number(state.selectedUserId) === Number(user.id) ? "active" : ""}`,
    onclick: async () => {
      state.selectedUserId = user.id;
      state.error = "";
      state.revisionNotice = "";
      try {
        await loadPersonas();
      } catch (err) {
        state.error = err.message;
      }
      render();
    },
  }, [
    h("span", { class: "avatar", text: String(label || "U").slice(0, 1).toUpperCase() }),
    h("span", {}, [
      h("strong", { text: label }),
      h("small", { text: `${user.role} / ${user.status}` }),
      queueText ? h("small", { class: pending ? "queue-active" : adjustment ? "queue-followup" : "queue-stale", text: queueText }) : null,
    ]),
  ]);
}

function renderMain() {
  return h("section", { class: "main" }, [
    h("header", { class: "topbar" }, [
      h("div", {}, [
        h("p", { class: "eyebrow", text: "Memory Review" }),
        h("h2", { text: "记忆审查台" }),
      ]),
      h("div", { class: "toolbar" }, [
        renderRevisionQueueSummary(),
        h("button", { type: "button", class: "ghost", text: "载入成长演示", onclick: seedGrowthDemo }),
        h("button", { type: "button", class: "ghost", text: "清除演示", onclick: clearGrowthDemo }),
        renderPersonaSelect(),
        h("button", { type: "button", class: "ghost", text: "刷新", onclick: refresh }),
      ]),
    ]),
    state.error ? h("p", { class: "error", text: state.error }) : null,
    state.growthDemo ? renderGrowthDemoNotice(state.growthDemo) : null,
    state.review ? renderReview() : renderEmpty(),
  ]);
}

function renderGrowthDemoNotice(demo) {
  return h("section", { class: "growth-demo-notice" }, [
    h("strong", { text: "人格成长演示已载入" }),
    h("p", { text: "当前选中的是可随时清除的演示账号。历史中已有一条由自动审核代理落实的聊天要求；点击“代理审核低风险聊天要求”会关闭一条当前已满足的旧积压。资料页写下的相处偏好已改为即时指导，不再进入人工队列。" }),
    h("p", { text: "也可以在普通端登录体验“相处痕迹”和主动偏好请求的公开状态。" }),
    h("p", { class: "demo-credentials", text: `普通端账号：${demo.username} / 密码：${demo.password}` }),
  ]);
}

function renderPersonaSelect() {
  const select = h("select", {
    onchange: async (event) => {
      state.selectedPersonaId = event.target.value ? Number(event.target.value) : null;
      state.error = "";
      state.revisionNotice = "";
      try {
        await loadReview();
      } catch (err) {
        state.error = err.message;
      }
      render();
    },
  });
  if (!state.personas.length) {
    select.append(h("option", { value: "", text: "暂无人格" }));
    return select;
  }
  for (const persona of state.personas) {
    const pending = Number(persona.pending_revision_count || 0);
    const requests = Number(persona.pending_preference_request_count || 0);
    const adjustment = Number(persona.adjustment_feedback_count || 0);
    const stale = Number(persona.stale_revision_count || 0);
    const suffix = [
      pending ? `待审 ${pending}` : "",
      requests ? `主动请求 ${requests}` : "",
      adjustment ? `待跟进 ${adjustment}` : "",
      stale ? `过期 ${stale}` : "",
    ].filter(Boolean).join(" / ");
    const option = h("option", { value: persona.id, text: `${persona.name}${suffix ? ` · ${suffix}` : ""}` });
    if (Number(persona.id) === Number(state.selectedPersonaId)) option.selected = true;
    select.append(option);
  }
  return select;
}

function renderRevisionQueueSummary() {
  const pending = state.personas.reduce((count, persona) => count + Number(persona.pending_revision_count || 0), 0);
  const requests = state.personas.reduce((count, persona) => count + Number(persona.pending_preference_request_count || 0), 0);
  const auto = state.personas.reduce((count, persona) => count + Number(persona.pending_auto_revision_count || 0), 0);
  const adjustment = state.personas.reduce((count, persona) => count + Number(persona.adjustment_feedback_count || 0), 0);
  const stale = state.personas.reduce((count, persona) => count + Number(persona.stale_revision_count || 0), 0);
  if (!pending && !adjustment && !stale) return null;
  const parts = [
    pending ? `待审 ${pending}` : "",
    requests ? `主动请求 ${requests}` : "",
    auto ? `代理可审 ${auto}` : "",
    adjustment ? `待跟进 ${adjustment}` : "",
    stale ? `过期 ${stale}` : "",
  ].filter(Boolean);
  return h("span", {
    class: `review-queue-badge ${pending ? "active" : adjustment ? "followup-only" : "stale-only"}`,
    title: "人格调整审核与反馈跟进队列",
    text: parts.join(" / "),
  });
}

function renderReview() {
  const review = state.review;
  return h("div", { class: "review-grid" }, [
    card("状态变量", renderStateList(review.state || [])),
    card("用户画像", renderInsight(review.insight || {})),
    card("摘要", renderLines((review.summaries || []).map((item) => item.text))),
    card("会话滚动摘要", renderConversationSummaries(review.conversation_summaries || []), "wide"),
    card("人格调整建议", renderRevisionPanel(), "wide"),
    card("轻表达使用", renderExpressionUsage(state.expressionUsage), "wide"),
    card("最近注入上下文", renderTraceList(state.traces || []), "wide"),
    card("当前事实", renderMemoryList(review.facts || []), "wide"),
    card("当前关系", renderMemoryList(review.relations || []), "wide"),
  ]);
}

function renderExpressionUsage(data) {
  if (!data) return h("p", { class: "muted", text: "暂无。" });
  const preference = data.preference || {};
  const assets = Array.isArray(state.expressionAssets) ? state.expressionAssets : [];
  const modeLabel = {
    off: "关闭",
    subtle: "克制",
    normal: "正常",
  }[preference.mode] || "正常";
  const counts = Array.isArray(data.counts) ? data.counts : [];
  const recent = Array.isArray(data.recent) ? data.recent : [];
  const summary = data.summary || {};
  const insights = Array.isArray(data.insights) ? data.insights : [];
  const reviewItems = Array.isArray(data.review_items) ? data.review_items : [];
  return h("div", { class: "expression-usage-panel" }, [
    h("div", { class: "expression-usage-head" }, [
      h("strong", { text: `当前模式：${modeLabel}` }),
      h("small", { text: preference.explicit ? "用户已显式设置" : "默认" }),
    ]),
    assets.length
      ? renderExpressionAssetCatalog(assets)
      : null,
    h("div", { class: "expression-usage-summary" }, [
      h("span", { text: `统计 ${summary.window || 0}` }),
      h("span", { text: `单聊 ${summary.single || 0}` }),
      h("span", { text: `群聊 ${summary.group || 0}` }),
      h("span", { text: `中风险 ${summary.medium_risk || 0}` }),
      h("span", { text: `已禁用历史 ${summary.disabled_asset || 0}` }),
    ]),
    insights.length
      ? h("div", { class: "expression-usage-insights" }, insights.map((item) => h("p", {
        class: `expression-usage-insight ${item.severity || "watch"}`,
        text: item.text || "",
      })))
      : null,
    reviewItems.length
      ? renderExpressionReviewItems(reviewItems)
      : null,
    counts.length
      ? h("div", { class: "expression-usage-counts" }, [
        h("small", { text: `标签计数基于 ${data.counted || counts.reduce((sum, item) => sum + Number(item.count || 0), 0)} 条历史` }),
        h("div", { class: "expression-usage-tags" }, counts.slice(0, 8).map((item) => h("span", {
          class: `expression-usage-tag ${item.asset_enabled === false ? "disabled" : "enabled"} ${item.risk_level || "low"}`,
          text: `${item.display_text || item.tag} × ${item.count}`,
        }))),
      ])
      : h("p", { class: "muted", text: "最近还没有展示轻表达。" }),
    recent.length
      ? h("div", { class: "expression-usage-list" }, recent.slice(0, 8).map(renderExpressionUsageItem))
      : null,
  ]);
}

function renderExpressionReviewItems(items) {
  const canApplyCooldowns = items.some((item) => Number.isFinite(Number(item.suggested_cooldown_turns)) && item.asset_enabled !== false);
  return h("div", { class: "expression-review-list" }, [
    h("div", { class: "expression-review-title" }, [
      h("strong", { text: "审查建议" }),
      canApplyCooldowns
        ? h("button", {
          type: "button",
          class: "ghost compact",
          text: "一键应用冷却建议",
          onclick: applyExpressionReviewCooldowns,
        })
        : null,
    ]),
    ...items.map((item) => h("article", { class: `expression-review-item ${item.severity || "watch"}` }, [
      h("div", { class: "expression-review-main" }, [
        h("span", { class: `expression-asset-risk ${item.risk_level || "unknown"}`, text: item.risk_level || "unknown" }),
        h("strong", { text: `${item.display_text || item.label || item.tag} × ${item.count || 0}` }),
        h("small", { text: `${item.group || "unknown"} / 占比 ${Math.round(Number(item.share || 0) * 100)}% / 冷却 ${item.cooldown_turns ?? 0} 轮` }),
      ]),
      h("p", { text: item.text || "" }),
      h("div", { class: "expression-review-actions" }, [
        Number.isFinite(Number(item.suggested_cooldown_turns))
          ? h("button", {
            type: "button",
            class: "ghost compact",
            text: `冷却到 ${item.suggested_cooldown_turns}`,
            onclick: () => applyExpressionReviewCooldown(item),
          })
          : null,
        h("button", {
          type: "button",
          class: "ghost compact",
          text: "备注",
          onclick: () => editExpressionReviewNote(item),
        }),
      ]),
    ])),
  ]);
}

function renderExpressionAssetCatalog(assets) {
  const counts = {
    all: assets.length,
    pending: assets.filter((asset) => asset.media_url && asset.media_review_status === "pending").length,
    rejected: assets.filter((asset) => asset.media_url && asset.media_review_status === "rejected").length,
    approved: assets.filter((asset) => asset.media_url && asset.media_review_status === "approved").length,
  };
  const filter = state.expressionAssetFilter || "all";
  const visibleAssets = assets
    .filter((asset) => {
      if (filter === "pending") return asset.media_url && asset.media_review_status === "pending";
      if (filter === "rejected") return asset.media_url && asset.media_review_status === "rejected";
      if (filter === "approved") return asset.media_url && asset.media_review_status === "approved";
      return true;
    })
    .slice()
    .sort((a, b) => expressionAssetReviewRank(a) - expressionAssetReviewRank(b));
  const groups = new Map();
  for (const asset of visibleAssets) {
    const key = asset.group || "general";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(asset);
  }
  const groupNodes = [...groups.entries()].map(([group, items]) => {
    const enabledCount = items.filter((item) => item.enabled !== false).length;
    return h("section", { class: "expression-asset-group" }, [
      h("div", { class: "expression-asset-group-head" }, [
        h("strong", { text: group }),
        h("small", { text: `${enabledCount}/${items.length} enabled` }),
      ]),
      h("div", { class: "expression-asset-group-list" }, items.map(renderExpressionAsset)),
    ]);
  });
  return h("div", { class: "expression-asset-catalog" }, [
    h("div", { class: "expression-asset-catalog-tools" }, [
      h("strong", { text: "表达资源" }),
      h("div", { class: "expression-asset-filter" }, [
        ...[
          ["all", `全部 ${counts.all}`],
          ["pending", `待审 ${counts.pending}`],
          ["rejected", `驳回 ${counts.rejected}`],
          ["approved", `已通过 ${counts.approved}`],
        ].map(([value, label]) => h("button", {
          type: "button",
          class: value === filter ? "compact" : "ghost compact",
          text: label,
          onclick: () => {
            state.expressionAssetFilter = value;
            render();
          },
        })),
        h("button", {
          type: "button",
          class: "ghost compact",
          text: "批量导入媒体",
          onclick: importExpressionAssetMediaBatch,
        }),
      ]),
    ]),
    groupNodes.length ? groupNodes : h("p", { class: "muted", text: "这个筛选下暂无表达资源。" }),
  ]);
}

function expressionAssetReviewRank(asset) {
  if (asset.media_url && asset.media_review_status === "pending") return 0;
  if (asset.media_url && asset.media_review_status === "rejected") return 1;
  if (asset.media_url && asset.media_review_status === "approved") return 2;
  return 3;
}

function renderExpressionAsset(asset) {
  const enabled = asset.enabled !== false;
  const lifecycle = asset.lifecycle_status || "active";
  const archived = lifecycle === "archived";
  const mediaUrl = asset.thumbnail_url || asset.media_url || "";
  return h("article", { class: `expression-asset-item ${asset.expression_type || "gesture"} ${enabled ? "enabled" : "disabled"}` }, [
    mediaUrl
      ? h("img", { class: "expression-asset-media", src: mediaUrl, alt: asset.alt_text || asset.display_text || asset.label || "", loading: "lazy" })
      : h("span", { class: "expression-asset-icon", text: asset.icon || "" }),
    h("span", {}, [
      h("strong", { text: asset.display_text || asset.label || "" }),
      h("small", { text: `${asset.expression_type || ""} / ${asset.group || "general"} / 强度 ${asset.intensity || 1} / 冷却 ${asset.cooldown_turns ?? 0} 轮 / ${enabled ? "启用" : "禁用"} / ${lifecycle} / 媒体${mediaReviewLabel(asset.media_review_status)}` }),
    ]),
    h("div", { class: "expression-asset-actions" }, [
      h("button", {
        type: "button",
        class: enabled ? "ghost compact" : "compact",
        text: enabled ? "禁用" : "启用",
        disabled: archived ? "disabled" : null,
        onclick: () => toggleExpressionAsset(asset),
      }),
      h("button", {
        type: "button",
        class: archived ? "compact" : "ghost compact",
        text: archived ? "恢复" : "归档",
        onclick: () => updateExpressionAssetLifecycle(asset, archived ? "active" : "archived"),
      }),
      h("button", {
        type: "button",
        class: "ghost compact",
        text: "备注",
        onclick: () => editExpressionAssetNote(asset),
      }),
      h("button", {
        type: "button",
        class: "ghost compact",
        text: "冷却",
        onclick: () => editExpressionAssetCooldown(asset),
      }),
      h("button", {
        type: "button",
        class: "ghost compact",
        text: "媒体",
        onclick: () => editExpressionAssetMedia(asset),
      }),
      h("button", {
        type: "button",
        class: "ghost compact",
        text: "上传",
        onclick: () => uploadExpressionAssetMedia(asset),
      }),
      mediaUrl && asset.media_review_status !== "approved"
        ? h("button", {
          type: "button",
          class: "ghost compact",
          text: "通过",
          onclick: () => updateExpressionAssetMediaReview(asset, "approved"),
        })
        : null,
      mediaUrl && asset.media_review_status !== "rejected"
        ? h("button", {
          type: "button",
          class: "ghost compact",
          text: "驳回",
          onclick: () => updateExpressionAssetMediaReview(asset, "rejected"),
        })
        : null,
    ]),
    h("p", { text: asset.description || "" }),
    h("small", { class: `expression-asset-risk ${asset.risk_level || "low"}`, text: `风险：${asset.risk_level || "low"}` }),
    asset.admin_note ? h("small", { class: "expression-asset-note", text: `备注：${asset.admin_note}` }) : null,
    asset.media_review_note ? h("small", { class: "expression-asset-note", text: `媒体审查：${asset.media_review_note}` }) : null,
    asset.media_source ? h("small", { class: "expression-asset-note", text: `媒体来源：${asset.media_source}${asset.media_source_detail ? ` · ${asset.media_source_detail}` : ""}` }) : null,
  ]);
}

function mediaReviewLabel(status) {
  return {
    pending: "待审",
    approved: "已通过",
    rejected: "已驳回",
  }[status] || "已通过";
}

async function toggleExpressionAsset(asset) {
  state.error = "";
  try {
    const data = await api(
      `/api/admin/expression-assets/${encodeURIComponent(asset.expression_type)}/${encodeURIComponent(asset.label)}`,
      {
        method: "PATCH",
        body: JSON.stringify({
          enabled: asset.enabled === false,
          admin_note: asset.enabled === false ? "管理台重新启用" : "管理台禁用",
        }),
      }
    );
    state.expressionAssets = data.assets || [];
    await loadReview();
  } catch (err) {
    state.error = err.message;
  }
  render();
}

async function updateExpressionAssetLifecycle(asset, lifecycleStatus) {
  state.error = "";
  try {
    const data = await api(
      `/api/admin/expression-assets/${encodeURIComponent(asset.expression_type)}/${encodeURIComponent(asset.label)}`,
      {
        method: "PATCH",
        body: JSON.stringify({
          enabled: lifecycleStatus === "active" ? true : asset.enabled !== false,
          lifecycle_status: lifecycleStatus,
          admin_note: lifecycleStatus === "archived" ? "管理台归档" : "管理台恢复为 active",
        }),
      }
    );
    state.expressionAssets = data.assets || [];
    await loadReview();
  } catch (err) {
    state.error = err.message;
  }
  render();
}

async function editExpressionAssetNote(asset) {
  const note = window.prompt("表达资源备注", asset.admin_note || "");
  if (note === null) return;
  state.error = "";
  try {
    const data = await api(
      `/api/admin/expression-assets/${encodeURIComponent(asset.expression_type)}/${encodeURIComponent(asset.label)}`,
      {
        method: "PATCH",
        body: JSON.stringify({
          enabled: asset.enabled !== false,
          admin_note: note,
        }),
      }
    );
    state.expressionAssets = data.assets || [];
    await loadReview();
  } catch (err) {
    state.error = err.message;
  }
  render();
}

async function editExpressionAssetCooldown(asset) {
  const current = Number.isFinite(Number(asset.cooldown_turns)) ? Number(asset.cooldown_turns) : 0;
  const value = window.prompt("冷却轮数（0-20）", String(current));
  if (value === null) return;
  const cooldown = Math.max(0, Math.min(20, Number.parseInt(value, 10)));
  if (!Number.isFinite(cooldown)) return;
  state.error = "";
  try {
    const data = await api(
      `/api/admin/expression-assets/${encodeURIComponent(asset.expression_type)}/${encodeURIComponent(asset.label)}`,
      {
        method: "PATCH",
        body: JSON.stringify({
          enabled: asset.enabled !== false,
          cooldown_turns: cooldown,
          admin_note: asset.admin_note || "",
        }),
      }
    );
    state.expressionAssets = data.assets || [];
    await loadReview();
  } catch (err) {
    state.error = err.message;
  }
  render();
}

async function editExpressionAssetMedia(asset) {
  const mediaUrl = window.prompt("媒体 URL（留空恢复为文本徽标）", asset.media_url || "");
  if (mediaUrl === null) return;
  const assetKind = mediaUrl.trim()
    ? window.prompt("媒体类型：image / gif / avatar_expression", asset.asset_kind === "text_badge" ? "image" : asset.asset_kind || "image")
    : "text_badge";
  if (assetKind === null) return;
  const thumbnailUrl = mediaUrl.trim()
    ? window.prompt("缩略图 URL（可留空使用媒体 URL）", asset.thumbnail_url || mediaUrl.trim())
    : "";
  if (thumbnailUrl === null) return;
  state.error = "";
  try {
    const data = await api(
      `/api/admin/expression-assets/${encodeURIComponent(asset.expression_type)}/${encodeURIComponent(asset.label)}`,
      {
        method: "PATCH",
        body: JSON.stringify({
          enabled: asset.enabled !== false,
          lifecycle_status: asset.lifecycle_status || "active",
          asset_kind: mediaUrl.trim() ? assetKind : "text_badge",
          media_url: mediaUrl.trim(),
          thumbnail_url: thumbnailUrl.trim(),
          alt_text: asset.alt_text || asset.display_text || asset.label || "",
          media_source: mediaUrl.trim() ? "manual_url" : "manual_clear",
          media_source_detail: mediaUrl.trim(),
          media_review_status: mediaUrl.trim() ? "approved" : "approved",
          media_review_note: mediaUrl.trim() ? "手动配置媒体 URL，自动批准" : "",
          admin_note: asset.admin_note || "",
        }),
      }
    );
    state.expressionAssets = data.assets || [];
    await loadReview();
  } catch (err) {
    state.error = err.message;
  }
  render();
}

async function updateExpressionAssetMediaReview(asset, status) {
  const note = status === "rejected"
    ? window.prompt("驳回原因", asset.media_review_note || "")
    : (asset.media_review_note || "管理台审核通过");
  if (note === null) return;
  state.error = "";
  try {
    const data = await api(
      `/api/admin/expression-assets/${encodeURIComponent(asset.expression_type)}/${encodeURIComponent(asset.label)}`,
      {
        method: "PATCH",
        body: JSON.stringify({
          enabled: asset.enabled !== false,
          lifecycle_status: asset.lifecycle_status || "active",
          asset_kind: asset.asset_kind || "text_badge",
          media_url: asset.media_url || "",
          thumbnail_url: asset.thumbnail_url || "",
          alt_text: asset.alt_text || asset.display_text || asset.label || "",
          media_source: asset.media_source || "",
          media_source_detail: asset.media_source_detail || "",
          media_review_status: status,
          media_review_note: note,
          admin_note: asset.admin_note || "",
        }),
      }
    );
    state.expressionAssets = data.assets || [];
    await loadReview();
  } catch (err) {
    state.error = err.message;
  }
  render();
}

function uploadExpressionAssetMedia(asset) {
  const input = h("input", {
    type: "file",
    accept: "image/png,image/jpeg,image/webp,image/gif",
  });
  input.style.position = "fixed";
  input.style.left = "-9999px";
  input.onchange = async () => {
    const file = input.files?.[0];
    input.remove();
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    const kind = file.type === "image/gif" ? "gif" : (asset.asset_kind === "avatar_expression" ? "avatar_expression" : "image");
    form.append("asset_kind", kind);
    state.error = "";
    try {
      const data = await api(
        `/api/admin/expression-assets/${encodeURIComponent(asset.expression_type)}/${encodeURIComponent(asset.label)}/upload`,
        {
          method: "POST",
          body: form,
        }
      );
      state.expressionAssets = data.assets || [];
      await loadReview();
    } catch (err) {
      state.error = err.message;
    }
    render();
  };
  document.body.append(input);
  input.click();
}

async function importExpressionAssetMediaBatch() {
  const example = JSON.stringify([
    {
      expression_type: "mood",
      label: "微笑",
      asset_kind: "image",
      media_url: "/uploads/expression-assets/smile.png",
      thumbnail_url: "/uploads/expression-assets/smile.png",
      alt_text: "微笑贴图",
    },
  ], null, 2);
  const text = window.prompt("批量导入媒体 JSON：可输入数组，或 {\"items\": [...]}。空 media_url 可恢复文本徽标。", example);
  if (text === null) return;
  let payload;
  try {
    const parsed = JSON.parse(text);
    payload = Array.isArray(parsed) ? { items: parsed } : parsed;
    if (!Array.isArray(payload.items)) throw new Error("items must be an array");
  } catch (err) {
    state.error = `JSON 无法解析：${err.message}`;
    render();
    return;
  }
  state.error = "";
  try {
    const data = await api("/api/admin/expression-assets/media/import", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    state.expressionAssets = data.assets || [];
    await loadReview();
    const failed = Number(data.failed_count || 0);
    window.alert(`已导入 ${data.imported_count || 0} 条${failed ? `，失败 ${failed} 条` : ""}`);
  } catch (err) {
    state.error = err.message;
  }
  render();
}

async function applyExpressionReviewCooldown(item) {
  const cooldown = Math.max(0, Math.min(20, Number.parseInt(item.suggested_cooldown_turns, 10)));
  if (!Number.isFinite(cooldown)) return;
  await updateExpressionAssetFromReview(item, {
    enabled: item.asset_enabled !== false,
    cooldown_turns: cooldown,
    admin_note: `管理台审查建议：冷却调整到 ${cooldown}`,
  });
}

async function editExpressionReviewNote(item) {
  const note = window.prompt("表达资源审查备注", "");
  if (note === null) return;
  await updateExpressionAssetFromReview(item, {
    enabled: item.asset_enabled !== false,
    admin_note: note,
  });
}

async function updateExpressionAssetFromReview(item, patch) {
  state.error = "";
  try {
    const data = await api(
      `/api/admin/expression-assets/${encodeURIComponent(item.expression_type)}/${encodeURIComponent(item.label)}`,
      {
        method: "PATCH",
        body: JSON.stringify(patch),
      }
    );
    state.expressionAssets = data.assets || [];
    await loadReview();
  } catch (err) {
    state.error = err.message;
  }
  render();
}

async function applyExpressionReviewCooldowns() {
  state.error = "";
  try {
    const data = await api("/api/admin/expression-review/apply-cooldowns", {
      method: "POST",
      body: JSON.stringify({
        target_user_id: state.selectedUserId,
        persona_id: state.selectedPersonaId,
        limit: 12,
        usage_limit: 80,
      }),
    });
    state.expressionAssets = data.assets || [];
    state.expressionUsage = data.expression_usage || state.expressionUsage;
    await loadReview();
  } catch (err) {
    state.error = err.message;
  }
  render();
}

function renderExpressionUsageItem(item) {
  const scope = item.scope === "group" ? "群聊" : "单聊";
  const enabled = item.asset_enabled !== false;
  const status = enabled ? "启用" : "已禁用";
  const label = item.display_text || item.label || "";
  const meta = `${scope} / ${item.persona_name || "TA"} / ${item.conversation_title || ""}`;
  return h("article", { class: "expression-usage-item" }, [
    h("div", { class: "memory-title" }, [
      h("strong", { text: `${item.icon || ""} ${label}`.trim() }),
      h("small", { text: meta }),
    ]),
    h("div", { class: "expression-usage-meta" }, [
      h("span", { class: `expression-asset-risk ${item.risk_level || "unknown"}`, text: `风险：${item.risk_level || "unknown"}` }),
      h("span", { text: `分组：${item.group || "unknown"}` }),
      h("span", { text: `冷却：${item.cooldown_turns ?? 0}轮` }),
      h("span", { text: `来源：${expressionSourceLabel(item.source_text)}` }),
      h("span", { class: enabled ? "asset-enabled" : "asset-disabled", text: status }),
    ]),
    h("p", { text: item.content || "" }),
  ]);
}

function expressionSourceLabel(sourceText) {
  const source = String(sourceText || "");
  if (source.startsWith("selection_agent:")) return `选择器 ${source.split(":").slice(1).join(":") || ""}`.trim();
  if (source.startsWith("[[expression:")) return "模型标签";
  if (source.startsWith("（") || source.startsWith("(")) return "括号兼容";
  return source ? "历史导入" : "未知";
}

function renderEmpty() {
  return h("section", { class: "empty" }, [
    h("h3", { text: "还没有可审查的记忆" }),
    h("p", { text: "先让这个用户和人格聊几句，记忆档案员会把重要内容写入分层记忆。" }),
  ]);
}

function card(title, body, extraClass = "") {
  return h("section", { class: `card ${extraClass}` }, [
    h("h3", { text: title }),
    body,
  ]);
}

function renderStateList(items) {
  if (!items.length) return h("p", { class: "muted", text: "暂无" });
  return h("ul", { class: "plain-list" }, items.map((item) => h("li", { text: `${item.key}: ${JSON.stringify(item.value)}` })));
}

function renderLines(lines) {
  if (!lines.length) return h("p", { class: "muted", text: "暂无" });
  return h("ul", { class: "plain-list" }, lines.slice(0, 12).map((line) => h("li", { text: line })));
}

function renderInsight(insight) {
  const topic = insight.topic_model || {};
  const guidance = insight.guidance || {};
  const discovery = insight.discovery_dimensions || {};
  const covered = discoveryCoverageLabels(discovery);
  const curiosity = curiosityFeedbackLabel(insight.curiosity_feedback || {});
  const lines = [
    insight.profile_summary ? `画像：${insight.profile_summary}` : "",
    listLine("喜欢", topic.likes),
    listLine("不喜欢", topic.dislikes),
    listLine("避开话题", topic.avoid_topics),
    listLine("探索覆盖", covered),
    curiosity,
    listLine("语气规则", guidance.tone_rules),
    listLine("话题规则", guidance.topic_rules),
    listLine("不要做", guidance.do_not),
  ].filter(Boolean);
  return lines.length ? renderLines(lines) : h("p", { class: "muted", text: "暂无画像。聊天几轮后用户画像器会逐步形成。" });
}

function renderConversationSummaries(items) {
  if (!items.length) return h("p", { class: "muted", text: "暂无。成功聊天后会自动生成会话摘要。" });
  return h("div", { class: "summary-list" }, items.map((item) => {
    const points = Array.isArray(item.key_points) ? item.key_points : [];
    return h("article", { class: "summary-item" }, [
      h("div", { class: "memory-title" }, [
        h("strong", { text: `Conversation #${item.conversation_id}` }),
        h("small", { text: `covered message ${item.covered_message_id || "-"}` }),
      ]),
      h("p", { text: item.summary_text || "暂无摘要" }),
      points.length ? h("ul", { class: "plain-list" }, points.slice(0, 8).map((point) => h("li", { text: point }))) : null,
    ]);
  }));
}

function listLine(label, values) {
  return Array.isArray(values) && values.length ? `${label}：${values.join("、")}` : "";
}

function renderMemoryList(items) {
  if (!items.length) return h("p", { class: "muted", text: "暂无" });
  return h("div", { class: "memory-list" }, items.slice(0, 40).map(renderMemoryItem));
}

function renderRevisionPanel() {
  const persona = currentPersona() || {};
  const cleanableStale = Number(persona.cleanable_stale_revision_count || 0);
  const hasReviewableChatFeedback = state.revisions.some((item) => (
    item.status === "pending" && item.origin === "explicit_feedback" && !item.stale
  ));
  return h("div", { class: "revision-panel" }, [
    h("div", { class: "inline-actions" }, [
      h("button", {
        type: "button",
        class: "ghost",
        text: state.generatingRevision ? "生成中" : "生成建议",
        onclick: () => generateRevision(),
        disabled: state.generatingRevision ? "disabled" : null,
      }),
      hasReviewableChatFeedback ? h("button", {
        type: "button",
        class: "ghost",
        text: state.autoReviewingRevisions ? "代理审核中" : "代理审核低风险聊天要求",
        onclick: () => autoReviewRevisions(),
        disabled: state.autoReviewingRevisions ? "disabled" : null,
      }) : null,
      cleanableStale ? h("button", {
        type: "button",
        class: "ghost",
        text: state.cleaningStaleRevisions ? "清理中" : `清理可关闭的过期建议 (${cleanableStale})`,
        onclick: () => dismissStaleRevisions(),
        disabled: state.cleaningStaleRevisions ? "disabled" : null,
      }) : null,
    ]),
    state.revisionNotice ? h("p", { class: "muted", text: state.revisionNotice }) : null,
    state.revisions.length
      ? h("div", { class: "revision-list" }, state.revisions.map(renderRevisionItem))
      : h("p", { class: "muted", text: "暂无建议。可以先让人格多聊几轮，或者直接生成一个保守建议。" }),
  ]);
}

function renderRevisionItem(item) {
  const suggestion = item.suggestion || {};
  const notes = Array.isArray(suggestion.change_notes) ? suggestion.change_notes : [];
  const changes = Array.isArray(item.changes) ? item.changes : [];
  const triggerUids = Array.isArray(item.trigger_memory_uids) ? item.trigger_memory_uids : [];
  const decisionNote = item.status === "pending"
    ? h("textarea", {
        class: "revision-decision-input",
        rows: "2",
        maxlength: "1000",
        placeholder: item.stale ? "记录为何忽略这条已过期建议（可选）" : "记录采纳或忽略原因（可选）",
      })
    : null;
  const source = item.source_context || {};
  const sourceMemories = [
    ...(source.feedback_facts || []),
    ...(source.feedback_relations || []),
  ].filter(Boolean);
  return h("details", { id: `revision-${item.id}`, class: `revision-item ${item.status} ${item.stale ? "stale" : ""}` }, [
    h("summary", {}, [
      h("strong", { text: `#${item.id} ${revisionStatusLabel(item.status)}` }),
      h("small", { text: revisionVersionText(item) }),
      h("span", { text: `${revisionOriginLabel(item)} · ${notes[0] || item.reason || "人格调整建议"}` }),
    ]),
    h("section", { class: "revision-body" }, [
      h("h4", { text: suggestion.name || "未命名" }),
      item.stale ? h("p", {
        class: "stale-notice",
        text: item.protected_by_active_request
          ? `${item.stale_reason || "这条建议已经过期。"} 它仍关联用户主动请求，请等待用户重新提交或撤回。`
          : `${item.stale_reason || "这条建议已经过期。"} 可忽略，或由上方按钮批量清理。`,
      }) : null,
      renderEvidenceOverview(item.evidence_summary || {}, item.base_version),
      item.trigger_message_id ? growthLine("触发消息", `message #${item.trigger_message_id}`) : null,
      triggerUids.length ? growthLine("触发记忆", triggerUids) : null,
      renderChangeList(changes, item.base_version ? `相对 v${item.base_version} 没有可显示的字段变化。` : "旧建议缺少可比较的基线版本。"),
      renderRevisionDecision(item),
      h("p", { text: suggestion.summary || "暂无摘要" }),
      h("p", { class: "muted", text: `关系：${suggestion.relationship || ""}` }),
      h("p", { class: "muted", text: `说话方式：${suggestion.speaking_style || ""}` }),
      suggestion.psychological_fit_notes ? h("p", { class: "muted", text: `心理适配：${suggestion.psychological_fit_notes}` }) : null,
      suggestion.growth_notes ? h("p", { class: "muted", text: `成长备注：${suggestion.growth_notes}` }) : null,
      notes.length ? h("ul", { class: "plain-list" }, notes.map((note) => h("li", { text: note }))) : null,
      sourceMemories.length ? renderGrowthMemoryList(sourceMemories.slice(0, 8)) : null,
      decisionNote ? h("label", { class: "revision-decision-editor" }, [
        h("span", { text: "审核备注" }),
        decisionNote,
      ]) : null,
      h("div", { class: "memory-actions" }, [
        item.status === "pending" && !item.stale ? h("button", { type: "button", class: "ghost", text: "应用", onclick: () => applyRevision(item.id, decisionNote.value) }) : null,
        item.status === "pending" ? h("button", { type: "button", class: "ghost", text: "忽略", onclick: () => dismissRevision(item.id, decisionNote.value) }) : null,
      ]),
    ]),
  ]);
}

function revisionVersionText(item) {
  if (item.applied_version) return `v${item.base_version || "?"} -> v${item.applied_version}`;
  if (item.stale) return `v${item.base_version || "?"} -> 已过期`;
  return `v${item.base_version || "?"} -> 待审核`;
}

function revisionStatusLabel(status) {
  const labels = {
    pending: "待审核",
    applied: "已应用",
    dismissed: "已忽略",
  };
  return labels[status] || status || "未知状态";
}

function revisionOriginLabel(item) {
  if (item.origin === "profile_request") return "资料页主动提交";
  if (item.origin === "explicit_core_update") return "聊天中明确设置";
  if (item.origin === "guidance_reconcile") return "指导失效同步";
  if (item.origin === "explicit_feedback") return item.trigger_message_id ? "聊天中提出" : "旧版主动提交";
  return "手动生成";
}

function renderRevisionDecision(item) {
  if (!item.decided_at && !item.decided_by_user_id && !item.decision_note) return null;
  const actor = item.decision_actor === "review_agent"
    ? "自动审核代理"
    : item.decision_actor === "adaptive_runtime"
      ? "自动适配"
    : item.decision_actor === "user"
      ? "用户本人"
      : item.decided_by_user_id
        ? `管理员 #${item.decided_by_user_id}`
        : "管理员";
  const metadata = [
    item.status === "applied" ? "已应用" : "已忽略",
    actor,
    item.decided_at ? formatTs(item.decided_at) : "",
  ].filter(Boolean).join(" / ");
  return h("section", { class: "revision-decision-record" }, [
    h("strong", { text: "审核决定" }),
    h("p", { text: item.decision_note || "未填写审核备注。" }),
    h("small", { text: metadata }),
  ]);
}

function renderEvidenceOverview(evidence, baseVersion) {
  const stateKeys = Array.isArray(evidence.state_keys) ? evidence.state_keys : [];
  const parts = [
    baseVersion ? `基线 v${baseVersion}` : "无基线",
    `${Number(evidence.memory_count || 0)} 条反馈/关系依据`,
    `${Number(evidence.summary_count || 0)} 条摘要`,
    `${Number(evidence.trace_count || 0)} 次近期上下文记录`,
  ];
  return h("section", { class: "evidence-summary" }, [
    h("strong", { text: "建议依据" }),
    h("p", { text: parts.join(" / ") }),
    stateKeys.length ? h("small", { text: `涉及状态：${stateKeys.join("、")}` }) : null,
  ]);
}

function renderChangeList(changes, emptyText) {
  return h("section", { class: "revision-diff" }, [
    h("strong", { text: "字段差异" }),
    changes.length
      ? h("div", { class: "revision-diff-list" }, changes.map((change) => h("div", { class: "revision-diff-row" }, [
        h("span", { text: change.label || change.field }),
        h("p", { class: "diff-before", text: change.before || "（空）" }),
        h("p", { class: "diff-after", text: change.after || "（空）" }),
      ])))
      : h("p", { class: "muted", text: emptyText }),
  ]);
}

function renderTraceList(traces) {
  if (!traces.length) return h("p", { class: "muted", text: "暂无。发送一次聊天后，这里会显示后台喂给聊天 AI 的记忆上下文。" });
  return h("div", { class: "trace-list" }, traces.map(renderTraceItem));
}

function renderTraceItem(trace) {
  const context = trace.context?.model_context || {};
  return h("details", { class: `trace-item ${trace.status}` }, [
    h("summary", {}, [
      h("strong", { text: `#${trace.id} ${trace.status}` }),
      h("small", { text: `prompt ${trace.prompt_chars} chars` }),
      h("span", { text: trace.query_text }),
    ]),
    renderPromptBlock("会话摘要", context.conversation_summary_prompt),
    renderPromptBlock("状态变量", context.state_prompt),
    renderPromptBlock("摘要", context.summary_prompt),
    renderPromptBlock("分层记忆", context.layered_prompt),
    renderPromptBlock("Semantic RAG", context.semantic_memory_prompt),
    renderPromptBlock("旧记忆召回", context.legacy_memory_prompt),
    renderPromptBlock("探索、边界与防重复策略", context.discovery_prompt),
    renderPromptBlock("资料按需使用与日期环境", context.profile_usage_prompt || context.calendar_prompt),
    trace.error_text ? h("p", { class: "error", text: trace.error_text }) : null,
  ]);
}

function renderPromptBlock(title, content) {
  return h("section", { class: "prompt-block" }, [
    h("h4", { text: title }),
    h("pre", { text: content || "暂无" }),
  ]);
}

function renderMemoryItem(item) {
  const text = item.text || [item.subject, item.predicate, item.object].filter(Boolean).join(" ");
  return h("article", { class: `memory-item ${item.archived ? "archived" : ""}` }, [
    h("div", {}, [
      h("div", { class: "memory-title" }, [
        h("strong", { text: item.uid }),
        h("small", { text: `${item.type || ""} / ${item.priority || "normal"} / ${item.confidence ?? ""}` }),
      ]),
      h("p", { text }),
      h("small", { text: item.valid_to ? `已被替代：${item.valid_to}` : "当前有效" }),
    ]),
    h("div", { class: "memory-actions" }, [
      h("button", { type: "button", class: "ghost", text: item.locked ? "解锁" : "锁定", onclick: () => patchMemory(item.uid, { locked: !item.locked }) }),
      h("button", { type: "button", class: "ghost", text: item.archived ? "恢复" : "归档", onclick: () => patchMemory(item.uid, { archived: !item.archived }) }),
    ]),
  ]);
}

async function patchMemory(uid, patch) {
  state.error = "";
  try {
    await api(`/api/admin/memory/items/${encodeURIComponent(uid)}?target_user_id=${state.selectedUserId}`, {
      method: "PATCH",
      body: JSON.stringify(patch),
    });
    await loadReview();
  } catch (err) {
    state.error = err.message;
  }
  render();
}

async function generateRevision(reason = "管理台手动发起人格调整复核") {
  if (!state.selectedPersonaId) return;
  state.error = "";
  state.revisionNotice = "";
  state.generatingRevision = true;
  render();
  try {
    await api(`/api/admin/persona-revisions?target_user_id=${state.selectedUserId}&persona_id=${state.selectedPersonaId}`, {
      method: "POST",
      body: JSON.stringify({ reason }),
    });
    await loadPersonas();
  } catch (err) {
    state.error = err.message;
  }
  state.generatingRevision = false;
  render();
}

async function applyRevision(id, note = "") {
  state.error = "";
  state.revisionNotice = "";
  try {
    await api(`/api/admin/persona-revisions/${id}/apply?target_user_id=${state.selectedUserId}`, {
      method: "POST",
      body: JSON.stringify({ note: note.trim() }),
    });
    await loadPersonas();
  } catch (err) {
    state.error = err.message;
  }
  render();
}

async function autoReviewRevisions() {
  if (!state.selectedPersonaId) return;
  state.error = "";
  state.revisionNotice = "";
  state.autoReviewingRevisions = true;
  render();
  try {
    const result = await api(`/api/admin/persona-revisions/auto-review?target_user_id=${state.selectedUserId}&persona_id=${state.selectedPersonaId}`, {
      method: "POST",
    });
    state.revisionNotice = result.applied_count
      ? `自动审核代理已应用 ${result.applied_count} 条低风险要求。`
      : result.dismissed_count
        ? `自动审核代理已关闭 ${result.dismissed_count} 条当前无需新增版本的要求。`
        : "没有可自动落实的新变化；关系或核心设定类记录只作为边界审计保留。";
    await loadPersonas();
  } catch (err) {
    state.error = err.message;
  }
  state.autoReviewingRevisions = false;
  render();
}

async function dismissRevision(id, note = "") {
  state.error = "";
  state.revisionNotice = "";
  try {
    await api(`/api/admin/persona-revisions/${id}/dismiss?target_user_id=${state.selectedUserId}`, {
      method: "POST",
      body: JSON.stringify({ note: note.trim() }),
    });
    await loadPersonas();
  } catch (err) {
    state.error = err.message;
  }
  render();
}

async function dismissStaleRevisions() {
  if (!state.selectedPersonaId) return;
  state.error = "";
  state.revisionNotice = "";
  state.cleaningStaleRevisions = true;
  render();
  try {
    await api(`/api/admin/persona-revisions/stale/dismiss?target_user_id=${state.selectedUserId}&persona_id=${state.selectedPersonaId}`, {
      method: "POST",
    });
    await loadPersonas();
  } catch (err) {
    state.error = err.message;
  }
  state.cleaningStaleRevisions = false;
  render();
}

async function restoreAdminPersonaVersion(version) {
  if (!state.selectedUserId || !state.selectedPersonaId) return;
  state.error = "";
  state.revisionNotice = "";
  try {
    const result = await api(
      `/api/admin/persona-versions/${version}/restore?target_user_id=${state.selectedUserId}&persona_id=${state.selectedPersonaId}`,
      {
        method: "POST",
        body: JSON.stringify({ note: "管理员从版本历史恢复" }),
      },
    );
    state.revisionNotice = `已恢复 v${version}，当前保存为 v${result.version}`;
    await loadPersonas();
  } catch (err) {
    state.error = err.message;
  }
  render();
}

function renderInsight(insight) {
  const topic = insight.topic_model || {};
  const guidance = insight.guidance || {};
  const covered = discoveryCoverageLabels(insight.discovery_dimensions || {});
  const curiosity = curiosityFeedbackLabel(insight.curiosity_feedback || {});
  if (state.editingInsight) return renderInsightEditor(insight);
  const lines = [
    insight.profile_summary ? `画像：${insight.profile_summary}` : "",
    listLine("喜欢", topic.likes),
    listLine("不喜欢", topic.dislikes),
    listLine("避开话题", topic.avoid_topics),
    listLine("探索覆盖", covered),
    curiosity,
    listLine("语气规则", guidance.tone_rules),
    listLine("话题规则", guidance.topic_rules),
    listLine("不要做", guidance.do_not),
  ].filter(Boolean);
  return h("div", { class: "insight-panel" }, [
    h("div", { class: "inline-actions" }, [
      h("button", { type: "button", class: "ghost", text: "编辑画像", onclick: () => { state.editingInsight = true; render(); } }),
    ]),
    lines.length ? renderLines(lines) : h("p", { class: "muted", text: "暂无画像。聊天几轮后用户画像器会逐步形成。" }),
  ]);
}

function discoveryCoverageLabels(discovery) {
  const labels = {
    interests: "兴趣与喜好",
    daily_rhythm: "日常节奏",
    values: "价值与在意的事",
    comfort_style: "安慰方式",
    boundaries: "边界与雷区",
    ambitions: "计划与期待",
    relationship_style: "相处期待",
  };
  return Object.entries(labels)
    .filter(([key]) => Number(discovery[key]?.observed_count || 0) > 0)
    .map(([key, label]) => `${label} (${discovery[key].observed_count})`);
}

function curiosityFeedbackLabel(feedback) {
  if (feedback.status === "cautious") {
    return `探索提问：谨慎，用户明确表示不希望被追问（${Number(feedback.declined_count || 0)} 次）`;
  }
  if (feedback.status === "invited") {
    return `探索提问：可自然询问，用户明确邀请了解（${Number(feedback.invited_count || 0)} 次）`;
  }
  return "";
}

function renderInsightEditor(insight) {
  const topic = insight.topic_model || {};
  const guidance = insight.guidance || {};
  const profile = h("textarea", { rows: "3" }, insight.profile_summary || "");
  const likes = h("textarea", { rows: "3" }, linesValue(topic.likes));
  const dislikes = h("textarea", { rows: "3" }, linesValue(topic.dislikes));
  const avoid = h("textarea", { rows: "3" }, linesValue(topic.avoid_topics));
  const safe = h("textarea", { rows: "3" }, linesValue(topic.safe_topics));
  const tone = h("textarea", { rows: "3" }, linesValue(guidance.tone_rules));
  const topicRules = h("textarea", { rows: "3" }, linesValue(guidance.topic_rules));
  const support = h("textarea", { rows: "3" }, linesValue(guidance.support_rules));
  const doNot = h("textarea", { rows: "3" }, linesValue(guidance.do_not));
  return h("form", {
    class: "insight-editor",
    onsubmit: async (event) => {
      event.preventDefault();
      await saveInsight({
        profile_summary: profile.value,
        interaction_style: insight.interaction_style || [],
        emotional_patterns: insight.emotional_patterns || [],
        inferred_profile: insight.inferred_profile || {},
        topic_model: {
          likes: parseLines(likes.value),
          dislikes: parseLines(dislikes.value),
          avoid_topics: parseLines(avoid.value),
          safe_topics: parseLines(safe.value),
        },
        guidance: {
          tone_rules: parseLines(tone.value),
          topic_rules: parseLines(topicRules.value),
          support_rules: parseLines(support.value),
          do_not: parseLines(doNot.value),
        },
      });
    },
  }, [
    editorField("画像摘要", profile),
    editorField("喜欢", likes),
    editorField("不喜欢", dislikes),
    editorField("避开话题", avoid),
    editorField("可自然回应的话题", safe),
    editorField("语气规则", tone),
    editorField("话题规则", topicRules),
    editorField("支持方式", support),
    editorField("不要做", doNot),
    h("div", { class: "inline-actions" }, [
      h("button", { type: "submit", text: "保存画像" }),
      h("button", { type: "button", class: "ghost", text: "取消", onclick: () => { state.editingInsight = false; render(); } }),
    ]),
  ]);
}

function editorField(label, control) {
  return h("label", { class: "editor-field" }, [label, control]);
}

function linesValue(values) {
  return Array.isArray(values) ? values.join("\n") : "";
}

function parseLines(value) {
  return String(value || "").split(/\n+/).map((line) => line.trim()).filter(Boolean);
}

async function saveInsight(payload) {
  state.error = "";
  try {
    const data = await api(`/api/admin/insight?target_user_id=${state.selectedUserId}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    });
    state.review.insight = data.insight;
    state.editingInsight = false;
    await loadReview();
  } catch (err) {
    state.error = err.message;
  }
  render();
}

function renderReview() {
  const review = state.review;
  return h("div", { class: "review-grid" }, [
    card("人格成长", renderGrowthPanel(), "wide"),
    card("状态变量", renderStateList(review.state || [])),
    card("模型调用记录", renderLlmCalls(), "wide"),
    card("模型路由", renderLlmRoutes(), "wide"),
    card("记忆评测", renderEvalPanel(), "wide"),
    card("用户画像", renderInsight(review.insight || {})),
    card("稳定摘要", renderLines((review.summaries || []).map((item) => item.text))),
    card("会话摘要", renderConversationSummaries(review.conversation_summaries || []), "wide"),
    card("记忆审校", renderJudgements(review.judgements || []), "wide"),
    card("记忆冲突", renderConflicts(review.conflicts || []), "wide"),
    card("人格调整建议", renderRevisionPanel(), "wide"),
    card("上下文追踪", renderTraceList(state.traces || []), "wide"),
    card("当前事实", renderMemoryList(review.facts || []), "wide"),
    card("当前关系", renderMemoryList(review.relations || []), "wide"),
  ]);
}

function renderLlmCalls() {
  const calls = state.llmCalls || [];
  if (!calls.length) return h("p", { class: "muted", text: "暂无模型调用记录。" });
  return h("div", { class: "llm-call-list" }, calls.map((item) => h("article", { class: `llm-call ${item.status}` }, [
    h("div", { class: "memory-title" }, [
      h("strong", { text: `${item.task} / ${item.status}` }),
      h("small", { text: `#${item.id} ${item.duration_ms}ms` }),
    ]),
    h("p", { text: `${item.provider} ${item.model}` }),
    h("small", { text: `prompt ${item.prompt_chars} chars / response ${item.response_chars} chars / ${formatTs(item.created_at)}` }),
    item.error_text ? h("pre", { text: item.error_text }) : null,
  ])));
}

function renderLlmRoutes() {
  const routes = state.llmRoutes?.effective || {};
  const entries = Object.entries(routes);
  if (!entries.length) {
    const fallback = state.llmRoutes?.default || {};
    return h("div", { class: "llm-route-list" }, [
      renderLlmRoute("default", fallback),
      h("p", { class: "muted", text: "暂无单独配置的任务路由。" }),
    ]);
  }
  return h("div", { class: "llm-route-list" }, entries.map(([task, config]) => renderLlmRoute(task, config)));
}

function renderLlmRoute(task, config = {}) {
  const envName = config.api_key_env || "";
  const envStatus = envName
    ? (config.api_key_env_present ? "env ready" : "env missing")
    : "no env key";
  return h("article", { class: "llm-route" }, [
    h("strong", { text: task }),
    h("p", { text: `${config.provider || "provider"} / ${config.model || "model"}` }),
    h("small", { text: [config.base_url, envName, envStatus, config.temperature !== undefined ? `temp ${config.temperature}` : "", config.timeout ? `timeout ${config.timeout}s` : "", config.max_tokens ? `max ${config.max_tokens}` : ""].filter(Boolean).join(" / ") }),
  ]);
}

function renderGrowthPanel() {
  const growth = state.growth || {};
  const persona = growth.persona || currentPersona() || {};
  const memories = [
    ...((growth.growth_memories || {}).facts || []),
    ...((growth.growth_memories || {}).relations || []),
  ];
  return h("div", { class: "growth-panel" }, [
    renderPersonaSnapshot(persona),
    h("section", { class: "growth-section" }, [
      h("h4", { text: "推动人格变化的记忆" }),
      memories.length ? renderGrowthMemoryList(memories) : h("p", { class: "muted", text: "暂无明确人格反馈、边界或关系期待记忆。" }),
    ]),
    h("section", { class: "growth-section" }, [
      h("h4", { text: "当前回应指导记录" }),
      (growth.preference_requests || []).length
        ? renderGrowthRequestHistory(growth.preference_requests, Number(persona.version || 0))
        : h("p", { class: "muted", text: "当前没有由用户设置或反馈形成的回应指导。" }),
    ]),
    h("section", { class: "growth-section" }, [
      h("h4", { text: "用户对已确认变化的反馈" }),
      (growth.user_feedback || []).length ? renderGrowthFeedbackHistory(growth.user_feedback) : h("p", { class: "muted", text: "用户尚未反馈已确认变化是否更合适。" }),
    ]),
    h("section", { class: "growth-section" }, [
      h("h4", { text: "版本历史" }),
      (state.versions || []).length ? renderVersionList(state.versions) : h("p", { class: "muted", text: "暂无版本记录。" }),
    ]),
  ]);
}

function renderPersonaSnapshot(persona) {
  const profile = persona.psychological_profile || {};
  return h("section", { class: "persona-snapshot" }, [
    h("div", { class: "snapshot-head" }, [
      h("div", {}, [
        h("h4", { text: persona.name || "未命名人格" }),
        h("small", { text: `v${persona.version || 1} / ${persona.relationship || "关系未定"}` }),
      ]),
      h("small", { text: persona.updated_at ? `updated ${formatTs(persona.updated_at)}` : "" }),
    ]),
    growthLine("摘要", persona.summary),
    growthLine("说话方式", persona.speaking_style),
    growthLine("外貌参考", persona.appearance_description || persona.desired_image),
    growthLine("心理适配", persona.psychological_fit_notes),
    growthLine("成长备注", persona.growth_notes || listText(profile.growth_direction)),
    growthLine("主要需要", listText(profile.primary_needs)),
    growthLine("安抚策略", listText(profile.comfort_strategy)),
    growthLine("避免模式", listText(profile.avoid_patterns)),
  ]);
}

function renderGrowthMemoryList(items) {
  return h("div", { class: "growth-memory-list" }, items.map((item) => {
    const text = item.text || [item.subject, item.predicate, item.object].filter(Boolean).join(" ");
    return h("article", { class: "growth-memory-item" }, [
      h("strong", { text: item.uid || item.predicate || item.type || "memory" }),
      h("p", { text }),
      h("small", { text: `${item.type || item.predicate || ""} / ${item.priority || "normal"} / importance ${Number(item.importance || 0).toFixed(2)}` }),
    ]);
  }));
}

function renderGrowthFeedbackHistory(items) {
  return h("div", { class: "growth-feedback-list" }, items.map(renderGrowthFeedbackItem));
}

function renderGrowthRequestHistory(items, currentVersion) {
  return h("div", { class: "growth-request-list" }, items.map((item) => {
    const pending = item.suggestion_status === "pending";
    const stale = pending && Number(item.base_version || 0) !== Number(currentVersion || 0);
    const status = Number(item.withdrawn_at || 0)
      ? item.deactivation_actor === "adaptive_runtime"
        ? "已由更新指导替代"
        : item.deactivation_actor === "chat_runtime"
          ? "已在聊天中停止"
        : "用户已停止"
      : !item.suggestion_id || item.suggestion_status === "dismissed"
        ? "自动指导中"
      : item.suggestion_status === "applied"
        ? "已形成变化"
        : stale
          ? "历史记录"
          : "自动指导中";
    return h("article", { class: `growth-request-item ${item.suggestion_status || "recorded"} ${stale ? "stale" : ""}`.trim() }, [
      h("div", { class: "growth-request-head" }, [
        h("strong", {
          text: item.request_origin === "growth_feedback"
            ? `反馈指导 #${item.id} · v${item.source_reviewed_version || "?"}`
            : item.request_origin === "chat_feedback"
              ? `聊天指导 #${item.id}`
              : `主动偏好 #${item.id}`,
        }),
        h("small", { class: pending && !stale ? "request-state" : "", text: status }),
      ]),
      h("p", { text: item.request_text || "" }),
      item.deactivation_reason ? h("p", { class: "muted", text: item.deactivation_reason }) : null,
      h("small", { text: item.created_at ? formatTs(item.created_at) : "" }),
      item.suggestion_id ? revisionLinkLine(item.suggestion_id) : null,
    ]);
  }));
}

function renderGrowthFeedbackItem(item) {
  const needsFollowup = item.reaction === "needs_adjustment" && !Number(item.resolved_at || 0);
  const note = needsFollowup
    ? h("textarea", {
        class: "revision-decision-input",
        rows: "2",
        maxlength: "1000",
        placeholder: "记录如何跟进了这次反馈（可选）",
      })
    : null;
  const resolutionMeta = item.resolved_at
    ? ["已跟进", item.resolved_by_user_id ? `管理员 #${item.resolved_by_user_id}` : "", formatTs(item.resolved_at)].filter(Boolean).join(" / ")
    : "";
  return h("article", { class: `growth-feedback-item ${item.reaction || ""} ${needsFollowup ? "open" : "resolved"}`.trim() }, [
    h("div", { class: "growth-feedback-head" }, [
      h("strong", { text: `v${item.reviewed_version} · ${item.reaction === "helpful" ? "这样更合适" : "还想调整"}` }),
      needsFollowup ? h("small", { class: "followup-state", text: "待跟进" }) : h("small", { text: item.updated_at ? formatTs(item.updated_at) : "" }),
    ]),
    resolutionMeta ? h("small", { text: resolutionMeta }) : null,
    item.detail_text ? h("p", { class: "growth-feedback-detail", text: `用户补充：${item.detail_text}` }) : null,
    item.resolution_note ? h("p", { text: item.resolution_note }) : null,
    note,
    note ? h("div", { class: "inline-actions" }, [
      h("button", {
        type: "button",
        class: "ghost",
        text: "据此生成建议",
        onclick: () => generateRevision(`跟进用户对已确认变化 v${item.reviewed_version} 的反馈：${item.detail_text || "用户标记仍需调整相处方式"}`),
      }),
      h("button", {
        type: "button",
        class: "ghost",
        text: "标记已跟进",
        onclick: () => resolveGrowthFeedback(item.reviewed_version, note.value),
      }),
    ]) : null,
  ]);
}

async function resolveGrowthFeedback(reviewedVersion, note = "") {
  state.error = "";
  try {
    await api(`/api/admin/persona-growth/feedback/resolve?target_user_id=${state.selectedUserId}&persona_id=${state.selectedPersonaId}`, {
      method: "POST",
      body: JSON.stringify({ reviewed_version: reviewedVersion, note: note.trim() }),
    });
    await loadPersonas();
  } catch (err) {
    state.error = err.message;
  }
  render();
}

function renderVersionList(items) {
  const currentVersion = Math.max(0, ...items.map((item) => Number(item.version || 0)));
  const versions = new Map(items.map((item) => [Number(item.version), item]));
  return h("div", { class: "version-list" }, items.map((item) => {
    const previous = versions.get(Number(item.version) - 1);
    const changes = previous ? personaFieldChanges(previous, item) : [];
    return h("details", { class: "version-item" }, [
    h("summary", {}, [
      h("strong", { text: `v${item.version}` }),
      h("span", { text: item.name || "未命名" }),
      h("small", { text: versionTypeLabel(item) }),
      Number(item.version) < currentVersion ? h("button", {
        type: "button",
        class: "ghost",
        text: "恢复此版",
        onclick: (event) => {
          event.preventDefault();
          event.stopPropagation();
          restoreAdminPersonaVersion(item.version);
        },
      }) : null,
    ]),
    h("div", { class: "version-body" }, [
      growthLine("摘要", item.summary),
      growthLine("关系", item.relationship),
      growthLine("说话方式", item.speaking_style),
      growthLine("心理适配", item.psychological_fit_notes),
      growthLine("成长备注", item.growth_notes),
      growthLine("变化类型", versionTypeLabel(item)),
      item.source_suggestion_id ? revisionLinkLine(item.source_suggestion_id) : null,
      growthLine("变化摘要", item.change_notes),
      previous ? renderChangeList(changes, `相对 v${previous.version} 没有可显示的字段变化。`) : null,
      growthLine("创建时间", item.created_at ? formatTs(item.created_at) : ""),
    ]),
  ]);
  }));
}

function versionTypeLabel(item) {
  const types = {
    initial_forge: "初始创建",
    user_profile_update: "用户编辑资料",
    sculptor_review: "审核应用的人格调整",
    user_version_restore: "恢复历史版本",
  };
  return types[item.change_type] || item.reason || "历史版本";
}

function revisionLinkLine(suggestionId) {
  return h("div", { class: "growth-line" }, [
    h("span", { text: "审核建议" }),
    h("p", {}, [h("a", {
      class: "evidence-link",
      href: `#revision-${suggestionId}`,
      text: `查看建议 #${suggestionId}`,
      onclick: (event) => focusRevisionSuggestion(event, suggestionId),
    })]),
  ]);
}

function focusRevisionSuggestion(event, suggestionId) {
  event.preventDefault();
  const target = document.getElementById(`revision-${suggestionId}`);
  if (!target) return;
  target.open = true;
  target.scrollIntoView({ behavior: "smooth", block: "center" });
}

function personaFieldChanges(before, after) {
  const fields = [
    ["name", "名字"],
    ["summary", "摘要"],
    ["relationship", "关系定位"],
    ["speaking_style", "说话方式"],
    ["traits", "人格特征"],
    ["boundaries", "边界"],
    ["psychological_fit_notes", "心理适配"],
    ["growth_notes", "成长备注"],
    ["appearance_description", "外貌参考"],
    ["desired_image", "期望形象"],
  ];
  return fields.flatMap(([field, label]) => {
    const oldValue = visibleValue(before?.[field]);
    const newValue = visibleValue(after?.[field]);
    return oldValue === newValue ? [] : [{ field, label, before: oldValue, after: newValue }];
  });
}

function visibleValue(value) {
  if (Array.isArray(value)) return value.join("、");
  if (value && typeof value === "object") return JSON.stringify(value);
  return String(value || "").trim();
}

function growthLine(label, value) {
  const text = Array.isArray(value) ? listText(value) : String(value || "").trim();
  if (!text) return null;
  return h("div", { class: "growth-line" }, [h("span", { text: label }), h("p", { text })]);
}

function listText(value) {
  return Array.isArray(value) && value.length ? value.join("、") : "";
}

function currentPersona() {
  return state.personas.find((item) => Number(item.id) === Number(state.selectedPersonaId)) || null;
}

function formatTs(value) {
  const date = new Date(Number(value) * 1000);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleString("zh-CN", { hour12: false });
}

function renderEvalPanel() {
  const latest = state.evalRuns?.[0];
  const result = latest?.results || {};
  const failed = (result.cases || []).filter((item) => item.required && !item.passed && !item.skipped);
  return h("div", { class: "eval-panel" }, [
    h("div", { class: "inline-actions" }, [
      h("button", { type: "button", text: state.runningEval ? "运行中..." : "运行核心评测", disabled: state.runningEval ? "disabled" : null, onclick: runMemoryEval }),
      h("button", { type: "button", class: "ghost", text: "聊天上下文评测", disabled: state.runningEval ? "disabled" : null, onclick: runChatContextEval }),
      h("button", { type: "button", class: "ghost", text: "资料上下文评测", disabled: state.runningEval ? "disabled" : null, onclick: runProfileContextEval }),
      h("button", { type: "button", class: "ghost", text: "状态解决评测", disabled: state.runningEval ? "disabled" : null, onclick: runStateResolutionEval }),
      h("button", { type: "button", class: "ghost", text: "状态过期评测", disabled: state.runningEval ? "disabled" : null, onclick: runStateExpiryEval }),
      h("button", { type: "button", class: "ghost", text: "策略评测", disabled: state.runningEval ? "disabled" : null, onclick: runMemoryPolicyEval }),
      h("button", { type: "button", class: "ghost", text: "实时回答评测", disabled: state.runningEval ? "disabled" : null, onclick: runLiveAnswerEval }),
      h("button", { type: "button", class: "ghost", text: "资料实时评测", disabled: state.runningEval ? "disabled" : null, onclick: runProfileLiveAnswerEval }),
      h("button", { type: "button", class: "ghost", text: "生成测试数据", onclick: seedMemoryEval }),
      h("button", { type: "button", class: "ghost", text: "运行语义召回评测", disabled: state.runningEval ? "disabled" : null, onclick: () => runMemoryEval(true) }),
    ]),
    latest ? h("div", { class: `eval-result ${latest.status}` }, [
      h("strong", { text: `${latest.suite_name}: ${latest.status}` }),
      h("small", { text: `score ${Number(latest.score || 0).toFixed(2)} / run #${latest.id}` }),
      h("small", { text: `passed ${result.passed || 0}/${result.total || 0}, semantic ${result.semantic_status || "skipped"}` }),
    ]) : h("p", { class: "muted", text: "尚未运行记忆评测。" }),
    result.reply ? h("details", { class: "eval-reply" }, [
      h("summary", { text: "最近一次实时回答" }),
      h("pre", { text: result.reply }),
    ]) : null,
    failed.length ? h("div", { class: "eval-failures" }, failed.map((item) => h("details", {}, [
      h("summary", { text: item.name }),
      h("pre", { text: JSON.stringify({ expected: item.expected, actual: item.actual }, null, 2) }),
    ]))) : null,
  ]);
}

async function seedMemoryEval() {
  state.error = "";
  try {
    const data = await api("/api/admin/evaluations/memory/seed", { method: "POST", body: JSON.stringify({}) });
    const users = await api("/api/admin/users");
    state.users = users.users;
    state.selectedUserId = data.seed.user_id;
    await loadPersonas();
  } catch (err) {
    state.error = err.message;
  }
  render();
}

async function seedGrowthDemo() {
  state.error = "";
  try {
    const data = await api("/api/admin/demos/persona-growth/seed", { method: "POST", body: JSON.stringify({}) });
    state.growthDemo = data.demo;
    state.selectedUserId = data.demo.user_id;
    await loadPersonas();
  } catch (err) {
    state.error = err.message;
  }
  render();
}

async function clearGrowthDemo() {
  state.error = "";
  try {
    await api("/api/admin/demos/persona-growth", { method: "DELETE" });
    state.growthDemo = null;
    const users = await api("/api/admin/users");
    state.users = users.users;
    state.selectedUserId = state.users.some((item) => Number(item.id) === Number(state.user?.id))
      ? state.user.id
      : state.users[0]?.id || null;
    await loadPersonas();
  } catch (err) {
    state.error = err.message;
  }
  render();
}

async function runMemoryEval(includeSemantic = false) {
  state.error = "";
  state.runningEval = true;
  render();
  try {
    const data = await api("/api/admin/evaluations/memory/run", {
      method: "POST",
      body: JSON.stringify({ reset_seed: true, include_semantic: includeSemantic }),
    });
    const users = await api("/api/admin/users");
    state.users = users.users;
    state.selectedUserId = data.run.seed.user_id;
    await loadPersonas();
  } catch (err) {
    state.error = err.message;
  }
  state.runningEval = false;
  render();
}

async function runChatContextEval() {
  state.error = "";
  state.runningEval = true;
  render();
  try {
    const data = await api("/api/admin/evaluations/chat-context/run", {
      method: "POST",
      body: JSON.stringify({ reset_seed: true, include_semantic: false }),
    });
    const users = await api("/api/admin/users");
    state.users = users.users;
    state.selectedUserId = data.run.seed.user_id;
    await loadPersonas();
  } catch (err) {
    state.error = err.message;
  }
  state.runningEval = false;
  render();
}

async function runLiveAnswerEval() {
  state.error = "";
  state.runningEval = true;
  render();
  try {
    const data = await api("/api/admin/evaluations/live-answer/run", {
      method: "POST",
      body: JSON.stringify({ reset_seed: true, include_semantic: false }),
    });
    const users = await api("/api/admin/users");
    state.users = users.users;
    state.selectedUserId = data.run.seed.user_id;
    await loadPersonas();
  } catch (err) {
    state.error = err.message;
  }
  state.runningEval = false;
  render();
}

async function runProfileContextEval() {
  state.error = "";
  state.runningEval = true;
  render();
  try {
    const data = await api("/api/admin/evaluations/profile-context/run", {
      method: "POST",
      body: JSON.stringify({ reset_seed: true, include_semantic: false }),
    });
    const users = await api("/api/admin/users");
    state.users = users.users;
    state.selectedUserId = data.run.seed.user_id;
    await loadPersonas();
  } catch (err) {
    state.error = err.message;
  }
  state.runningEval = false;
  render();
}

async function runProfileLiveAnswerEval() {
  state.error = "";
  state.runningEval = true;
  render();
  try {
    const data = await api("/api/admin/evaluations/profile-live-answer/run", {
      method: "POST",
      body: JSON.stringify({ reset_seed: true, include_semantic: false }),
    });
    const users = await api("/api/admin/users");
    state.users = users.users;
    state.selectedUserId = data.run.seed.user_id;
    await loadPersonas();
  } catch (err) {
    state.error = err.message;
  }
  state.runningEval = false;
  render();
}

async function runStateResolutionEval() {
  state.error = "";
  state.runningEval = true;
  render();
  try {
    const data = await api("/api/admin/evaluations/state-resolution/run", {
      method: "POST",
      body: JSON.stringify({ reset_seed: true, include_semantic: false }),
    });
    const users = await api("/api/admin/users");
    state.users = users.users;
    state.selectedUserId = data.run.seed.user_id;
    await loadPersonas();
  } catch (err) {
    state.error = err.message;
  }
  state.runningEval = false;
  render();
}

async function runStateExpiryEval() {
  state.error = "";
  state.runningEval = true;
  render();
  try {
    const data = await api("/api/admin/evaluations/state-expiry/run", {
      method: "POST",
      body: JSON.stringify({ reset_seed: true, include_semantic: false }),
    });
    const users = await api("/api/admin/users");
    state.users = users.users;
    state.selectedUserId = data.run.seed.user_id;
    await loadPersonas();
  } catch (err) {
    state.error = err.message;
  }
  state.runningEval = false;
  render();
}

async function runMemoryPolicyEval() {
  state.error = "";
  state.runningEval = true;
  render();
  try {
    const data = await api("/api/admin/evaluations/memory-policy/run", {
      method: "POST",
      body: JSON.stringify({ reset_seed: true, include_semantic: false }),
    });
    const users = await api("/api/admin/users");
    state.users = users.users;
    state.selectedUserId = data.run.seed.user_id;
    await loadPersonas();
  } catch (err) {
    state.error = err.message;
  }
  state.runningEval = false;
  render();
}

function renderJudgements(items) {
  if (!items.length) return h("p", { class: "muted", text: "暂无待处理的记忆审校。" });
  return h("div", { class: "judgement-list" }, items.map(renderJudgementItem));
}

function renderConflicts(items) {
  if (!items.length) return h("p", { class: "muted", text: "暂无待处理的记忆冲突。" });
  return h("div", { class: "conflict-list" }, items.map(renderConflictItem));
}

function renderConflictItem(item) {
  return h("article", { class: `conflict-item resolution-${item.resolution}` }, [
    h("div", { class: "memory-title" }, [
      h("strong", { text: `${item.conflict_type} / ${item.resolution}` }),
      h("small", { text: `#${item.id}` }),
    ]),
    h("div", { class: "conflict-compare" }, [
      h("section", {}, [
        h("small", { text: `current ${item.current_uid}` }),
        h("p", { text: item.current_text || "" }),
      ]),
      h("section", {}, [
        h("small", { text: `previous ${item.previous_uid || "-"}` }),
        h("p", { text: item.previous_text || "" }),
      ]),
    ]),
    item.reason ? h("small", { text: item.reason }) : null,
    h("div", { class: "memory-actions" }, [
      h("button", { type: "button", class: "ghost", text: "标记已解决", onclick: () => setConflictStatus(item.id, "resolved") }),
      h("button", { type: "button", class: "ghost", text: "忽略", onclick: () => setConflictStatus(item.id, "dismissed") }),
    ]),
  ]);
}

async function setConflictStatus(id, status) {
  state.error = "";
  try {
    await api(`/api/admin/memory/conflicts/${id}?target_user_id=${state.selectedUserId}`, {
      method: "PATCH",
      body: JSON.stringify({ status }),
    });
    await loadReview();
  } catch (err) {
    state.error = err.message;
  }
  render();
}

function renderJudgementItem(item) {
  const reasons = Array.isArray(item.reasons) ? item.reasons : [];
  const flags = Array.isArray(item.flags) ? item.flags : [];
  return h("article", { class: `judgement-item action-${item.action}` }, [
    h("div", { class: "memory-title" }, [
      h("strong", { text: `${item.memory_uid} / ${item.action}` }),
      h("small", { text: `quality ${Number(item.quality_score || 0).toFixed(2)} / risk ${Number(item.risk_score || 0).toFixed(2)}` }),
    ]),
    h("p", { text: item.memory_text || "" }),
    flags.length ? h("small", { text: `flags: ${flags.join(", ")}` }) : null,
    reasons.length ? h("ul", { class: "plain-list" }, reasons.map((reason) => h("li", { text: reason }))) : null,
    h("div", { class: "memory-actions" }, [
      h("button", { type: "button", class: "ghost", text: "接受", onclick: () => setJudgementStatus(item.id, "accepted") }),
      h("button", { type: "button", class: "ghost", text: "忽略", onclick: () => setJudgementStatus(item.id, "dismissed") }),
    ]),
  ]);
}

async function setJudgementStatus(id, status) {
  state.error = "";
  try {
    await api(`/api/admin/memory/judgements/${id}?target_user_id=${state.selectedUserId}`, {
      method: "PATCH",
      body: JSON.stringify({ status }),
    });
    await loadReview();
  } catch (err) {
    state.error = err.message;
  }
  render();
}

async function refresh() {
  state.error = "";
  try {
    await loadPersonas();
  } catch (err) {
    state.error = err.message;
  }
  render();
}

bootstrap();
