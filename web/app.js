const app = document.getElementById("app");
const TAB_SESSION_MODE_KEY = "mnemosyne:tab-session-mode";
const TAB_SESSION_TOKEN_KEY = "mnemosyne:tab-session-token";
const GROUP_AUTO_CHECK_MS = 30000;
const GROUP_AUTO_MIN_IDLE_SECONDS = 75;
const GROUP_AUTO_USER_WINDOW_SECONDS = 600;
const GROUP_AUTO_FAILURE_BACKOFF_SECONDS = 300;

const groupAutoCooldowns = new Map();

const text = {
  appName: "忆界树",
  appSubtitle: "Project Mnemosyne",
  login: "登录",
  register: "注册",
  logout: "退出",
  username: "账号",
  password: "密码",
  nickname: "昵称",
  birthday: "生日",
  signature: "个性签名",
  save: "保存",
  send: "发送",
  waiting: "生成中",
  createPersona: "创建人格",
  wizardTitle: "你想和什么样的人交谈？",
  optionalDescription: "自由描述",
  directGenerate: "直接生成",
  messagePlaceholder: "说点什么",
  profile: "资料",
  chats: "聊天",
  adminConsole: "管理台",
  newChat: "新对话",
  backToChat: "返回聊天",
};

const starterPrompts = [
  "我希望 TA 记得我说过的小事，但不要像客服一样总结我。",
  "聊天可以轻松一点，能吐槽；遇到认真事情时要稳。",
  "TA 可以主动关心我，但不要一直追问，也不要说教。",
  "我不确定具体人设，只希望 TA 能慢慢适应我的情绪节奏。",
];

let state = {
  user: null,
  profile: null,
  options: null,
  expressionAssets: [],
  personas: [],
  deletedPersonas: [],
  personaGrowth: {},
  loadingGrowthPersonaId: null,
  conversations: [],
  archivedConversations: [],
  groupConversations: [],
  archivedGroupConversations: [],
  activePersona: null,
  activeConversationId: null,
  activeGroupConversationId: null,
  activeGroupConversation: null,
  messages: [],
  groupMessages: [],
  view: "chat",
  editingPersona: false,
  profileOpen: false,
  personaPanelOpen: false,
  threadPanelOpen: false,
  deletedPanelOpen: false,
  groupCreateOpen: false,
  groupSettingsOpen: false,
  editingConversationId: null,
  showArchived: false,
  showArchivedGroups: false,
  conversationSearch: "",
  focusComposer: false,
  sending: false,
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

function prepareAuthMode(isolated) {
  try {
    if (isolated) {
      sessionStorage.setItem(TAB_SESSION_MODE_KEY, "1");
      sessionStorage.removeItem(TAB_SESSION_TOKEN_KEY);
    } else {
      sessionStorage.removeItem(TAB_SESSION_MODE_KEY);
      sessionStorage.removeItem(TAB_SESSION_TOKEN_KEY);
    }
  } catch {
    // Session storage may be unavailable in restricted browser modes.
  }
}

function acceptAuthSession(data, isolated) {
  prepareAuthMode(isolated);
  if (!isolated) return;
  if (!data.tab_session_token) {
    throw new Error("服务端尚未加载独立登录功能，请重启程序后再试。");
  }
  try {
    sessionStorage.setItem(TAB_SESSION_TOKEN_KEY, data.tab_session_token);
  } catch {
    throw new Error("浏览器无法保存本页登录状态，请取消独立登录后重试。");
  }
}

async function api(path, options = {}) {
  const isolated = tabSessionMode();
  const token = tabSessionToken();
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (isolated && token) headers.Authorization = `Bearer ${token}`;
  const res = await fetch(path, {
    ...options,
    credentials: isolated ? "omit" : "same-origin",
    headers,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(data.detail || `HTTP ${res.status}`);
  }
  return data;
}

async function uploadAvatarFile(file) {
  const form = new FormData();
  form.append("file", file);
  const isolated = tabSessionMode();
  const token = tabSessionToken();
  const headers = {};
  if (isolated && token) headers.Authorization = `Bearer ${token}`;
  const res = await fetch("/api/uploads/avatar", {
    method: "POST",
    credentials: isolated ? "omit" : "same-origin",
    headers,
    body: form,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(data.detail || `HTTP ${res.status}`);
  }
  return data.url;
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
    state.user = me.user;
    state.profile = me.profile;
    await loadMainData({ openLatest: true });
    renderShell();
    scrollChat();
  } catch {
    renderAuth("login");
  }
}

function renderAuth(mode) {
  const isRegister = mode === "register";
  const form = h("form", { class: "auth-card" });
  const isolatedLogin = h("input", { type: "checkbox" });
  isolatedLogin.checked = tabSessionMode();
  form.append(
    h("p", { class: "eyebrow", text: "Project Mnemosyne" }),
    h("h1", { text: isRegister ? text.register : text.login }),
    h("label", {}, [text.username, h("input", { name: "username", required: "required", autocomplete: "username" })]),
    h("label", {}, [
      text.password,
      h("input", {
        name: "password",
        type: "password",
        required: "required",
        minlength: "8",
        autocomplete: isRegister ? "new-password" : "current-password",
      }),
    ])
  );
  if (isRegister) {
    form.append(h("label", {}, [text.nickname, h("input", { name: "nickname", autocomplete: "nickname" })]));
  }
  form.append(h("label", { class: "auth-session-option" }, [
    isolatedLogin,
    h("span", {}, [
      h("strong", { text: "此标签页独立登录" }),
      h("small", { text: "同一浏览器同时测试多个账号时使用" }),
    ]),
  ]));
  form.append(
    h("button", { type: "submit", text: isRegister ? text.register : text.login }),
    h("button", {
      type: "button",
      class: "ghost",
      text: isRegister ? text.login : text.register,
      onclick: () => renderAuth(isRegister ? "login" : "register"),
    }),
    h("button", {
      type: "button",
      class: "ghost",
      text: "游客体验（3天）",
      onclick: async () => {
        error.textContent = "";
        try {
          const isolated = isolatedLogin.checked;
          prepareAuthMode(isolated);
          const data = await api(`/api/auth/guest?tab_session=${isolated ? "true" : "false"}`, { method: "POST" });
          acceptAuthSession(data, isolated);
          state.user = data.user;
          state.profile = data.profile;
          await loadMainData({ openLatest: true });
          renderShell();
          scrollChat();
        } catch (err) {
          error.textContent = err.message;
        }
      },
    })
  );

  const error = h("p", { class: "error" });
  form.append(error);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    error.textContent = "";
    const body = Object.fromEntries(new FormData(form).entries());
    try {
      const isolated = isolatedLogin.checked;
      prepareAuthMode(isolated);
      body.tab_session = isolated;
      const data = await api(isRegister ? "/api/auth/register" : "/api/auth/login", {
        method: "POST",
        body: JSON.stringify(body),
      });
      acceptAuthSession(data, isolated);
      state.user = data.user;
      state.profile = data.profile;
      await loadMainData({ openLatest: true });
      renderShell();
      scrollChat();
    } catch (err) {
      error.textContent = err.message;
    }
  });

  app.className = "auth-screen";
  app.replaceChildren(form);
}

async function loadMainData({ openLatest = false } = {}) {
  const [options, expressionAssets, personas, deletedPersonas, conversations, archivedConversations, groupConversations, archivedGroupConversations] = await Promise.all([
    api("/api/persona-options"),
    api("/api/expression-assets").catch(() => ({ assets: [] })),
    api("/api/personas"),
    api("/api/personas/deleted"),
    api("/api/conversations?status=active"),
    api("/api/conversations?status=archived"),
    api("/api/group-conversations?status=active"),
    api("/api/group-conversations?status=archived"),
  ]);
  state.options = options.options;
  state.expressionAssets = expressionAssets.assets || [];
  state.personas = personas.personas;
  state.deletedPersonas = deletedPersonas.personas;
  state.conversations = conversations.conversations;
  state.archivedConversations = archivedConversations.conversations;
  state.groupConversations = groupConversations.group_conversations || [];
  state.archivedGroupConversations = archivedGroupConversations.group_conversations || [];
  if (state.activeGroupConversationId) {
    state.activeGroupConversation = [...state.groupConversations, ...state.archivedGroupConversations]
      .find((item) => Number(item.id) === Number(state.activeGroupConversationId)) || state.activeGroupConversation;
  }
  const lastActive = loadLastActive();

  if (state.view === "group" && state.activeGroupConversationId) {
    state.activePersona = null;
  } else if (state.activePersona) {
    const refreshed = state.personas.find((persona) => Number(persona.id) === Number(state.activePersona.id));
    state.activePersona = refreshed || state.personas[0] || null;
  } else if (openLatest && lastActive.personaId) {
    state.activePersona = state.personas.find((persona) => Number(persona.id) === Number(lastActive.personaId)) || null;
  } else if (openLatest && lastActive.conversationId) {
    const conversation = state.conversations.find((item) => Number(item.id) === Number(lastActive.conversationId));
    state.activePersona = state.personas.find((persona) => Number(persona.id) === Number(conversation?.persona_id)) || null;
  } else if (openLatest && state.conversations.length) {
    state.activePersona = state.personas.find((persona) => Number(persona.id) === Number(state.conversations[0].persona_id)) || null;
  } else {
    state.activePersona = state.personas[0] || null;
  }
  state.view = state.activePersona || state.activeGroupConversation ? state.view : "forge";
  if (openLatest && state.personas.length) {
    state.view = "home";
    state.activeConversationId = null;
    state.messages = [];
    return;
  }
  if (openLatest && state.activePersona) {
    await openInitialConversation();
  }
}

async function loadConversationMessages(conversationId) {
  const data = await api(`/api/conversations/${conversationId}/messages`);
  state.messages = data.messages;
}

async function loadGroupMessages(groupConversationId) {
  const data = await api(`/api/group-conversations/${groupConversationId}/messages`);
  state.groupMessages = data.messages;
}

function returnToHome() {
  state.view = "home";
  state.activePersona = null;
  state.activeConversationId = null;
  state.activeGroupConversationId = null;
  state.activeGroupConversation = null;
  state.messages = [];
  state.groupMessages = [];
  state.threadPanelOpen = false;
  state.personaPanelOpen = false;
  state.groupSettingsOpen = false;
  renderShell();
  loadMainData().then(() => renderShell()).catch((err) => {
    state.error = err.message;
    renderShell();
  });
}

function isMobileShell() {
  return Boolean(window.matchMedia?.("(max-width: 1024px), (hover: none) and (pointer: coarse)").matches);
}

async function openConversationItem(item) {
  const persona = state.personas.find((entry) => Number(entry.id) === Number(item.persona_id));
  if (persona) state.activePersona = persona;
  state.activeConversationId = item.id;
  state.activeGroupConversationId = null;
  state.activeGroupConversation = null;
  state.groupMessages = [];
  state.view = "chat";
  state.editingPersona = false;
  state.personaPanelOpen = false;
  state.threadPanelOpen = false;
  await loadConversationMessages(item.id);
  await loadMainData();
  renderShell();
  scrollChat();
}

async function openGroupConversationItem(item) {
  state.activeGroupConversation = item;
  state.activeGroupConversationId = item.id;
  state.activePersona = null;
  state.activeConversationId = null;
  state.messages = [];
  state.view = "group";
  state.editingPersona = false;
  state.personaPanelOpen = false;
  state.threadPanelOpen = false;
  await loadGroupMessages(item.id);
  await loadMainData();
  renderShell();
  scrollChat();
}

async function openPersonaItem(persona) {
  state.activePersona = persona;
  state.activeGroupConversationId = null;
  state.activeGroupConversation = null;
  state.groupMessages = [];
  state.view = "chat";
  state.editingPersona = false;
  state.personaPanelOpen = false;
  state.threadPanelOpen = false;
  await openLatestConversationForPersona(persona.id);
  renderShell();
  scrollChat();
}

async function openPersonaPanel() {
  const personaId = Number(state.activePersona?.id);
  if (!personaId) return;
  state.personaPanelOpen = true;
  state.editingPersona = false;
  state.loadingGrowthPersonaId = personaId;
  renderShell();
  try {
    const data = await api(`/api/personas/${personaId}/growth`);
    state.personaGrowth[personaId] = data.growth;
    if (data.growth?.latest_reviewed_change?.unseen) {
      try {
        await api(`/api/personas/${personaId}/growth/viewed`, { method: "POST" });
        clearGrowthNotice(personaId);
      } catch {
        // Keep the notice visible if acknowledgement cannot be persisted.
      }
    }
  } catch {
    // Growth feedback is supplementary; profile details remain usable if it fails.
  } finally {
    if (Number(state.loadingGrowthPersonaId) === personaId) {
      state.loadingGrowthPersonaId = null;
    }
    if (state.personaPanelOpen && Number(state.activePersona?.id) === personaId) renderShell();
  }
}

async function openPersonaGrowthNotice(event, persona = state.activePersona) {
  event?.preventDefault();
  event?.stopPropagation();
  if (persona) state.activePersona = persona;
  await openPersonaPanel();
}

function clearGrowthNotice(personaId) {
  state.personas = state.personas.map((persona) => (
    Number(persona.id) === Number(personaId) ? { ...persona, growth_notice: null } : persona
  ));
  if (Number(state.activePersona?.id) === Number(personaId)) {
    state.activePersona = { ...state.activePersona, growth_notice: null };
  }
}

function clearGrowthAction(personaId) {
  state.personas = state.personas.map((persona) => (
    Number(persona.id) === Number(personaId) ? { ...persona, growth_action: null } : persona
  ));
  if (Number(state.activePersona?.id) === Number(personaId)) {
    state.activePersona = { ...state.activePersona, growth_action: null };
  }
}

async function openLatestConversationForPersona(personaId) {
  const latest = latestConversationForPersona(personaId);
  state.activeConversationId = latest?.id || null;
  if (latest) {
    await loadConversationMessages(latest.id);
  } else {
    state.messages = [];
  }
}

async function openInitialConversation() {
  const lastActive = loadLastActive();
  let conversation = null;
  if (lastActive.conversationId) {
    conversation = state.conversations.find((item) => Number(item.id) === Number(lastActive.conversationId)) || null;
  }
  if (!conversation && state.activePersona) {
    conversation = latestConversationForPersona(state.activePersona.id);
  }
  if (!conversation) {
    conversation = state.conversations[0] || null;
  }
  if (conversation) {
    const persona = state.personas.find((item) => Number(item.id) === Number(conversation.persona_id));
    if (persona) state.activePersona = persona;
    state.activeConversationId = conversation.id;
    await loadConversationMessages(conversation.id);
  } else {
    state.activeConversationId = null;
    state.messages = [];
  }
}

function latestConversationForPersona(personaId) {
  return state.conversations
    .filter((item) => Number(item.persona_id) === Number(personaId))
    .sort((left, right) => Number(right.updated_at || 0) - Number(left.updated_at || 0))[0] || null;
}

function lastActiveKey() {
  return `mnemosyne:last-active:${state.user?.id || "anonymous"}`;
}

function loadLastActive() {
  try {
    return JSON.parse(localStorage.getItem(lastActiveKey()) || "{}");
  } catch {
    return {};
  }
}

function saveLastActive() {
  if (!state.user) return;
  try {
    if (state.view === "group" && state.activeGroupConversationId) {
      localStorage.setItem(
        lastActiveKey(),
        JSON.stringify({
          mode: "group",
          groupConversationId: state.activeGroupConversationId,
        })
      );
      return;
    }
    if (!state.activePersona) return;
    localStorage.setItem(
      lastActiveKey(),
      JSON.stringify({
        mode: "chat",
        personaId: state.activePersona.id,
        conversationId: state.activeConversationId,
      })
    );
  } catch {
    // Local storage may be unavailable in restricted browser modes.
  }
}

function renderShell() {
  saveLastActive();
  const restoreSearchFocus = document.activeElement?.classList?.contains("conversation-search");
  app.className = `app-shell view-${state.view || "chat"} ${state.activePersona || state.activeGroupConversation ? "has-persona" : "no-persona"}`;
  const children = [renderSidebar(), renderMain()];
  if (state.profileOpen) children.push(renderProfileModal());
  if (state.personaPanelOpen) children.push(renderPersonaModal());
  if (state.threadPanelOpen) children.push(renderThreadModal());
  if (state.deletedPanelOpen) children.push(renderDeletedPersonasModal());
  if (state.groupCreateOpen) children.push(renderGroupCreateModal());
  app.replaceChildren(...children);
  if (restoreSearchFocus) {
    requestAnimationFrame(() => {
      const input = document.querySelector(".conversation-search");
      if (input) {
        input.focus();
        input.setSelectionRange(input.value.length, input.value.length);
      }
    });
  }
}

function renderSidebar() {
  return h("aside", { class: "sidebar" }, [
    renderBrand(),
    h("section", { class: "sidebar-section chat-section" }, [
      h("div", { class: "section-head" }, [
        h("strong", { text: text.chats }),
        h("button", {
          type: "button",
          class: "icon-btn",
          title: text.createPersona,
          "aria-label": text.createPersona,
          text: "+",
          onclick: () => {
            state.view = "forge";
            renderShell();
          },
        }),
      ]),
      renderConversationSearch(),
      renderChatList(),
    ]),
    renderUserEntry(),
    isAdmin()
      ? h("button", {
          type: "button",
          class: "ghost full",
          text: text.adminConsole,
          onclick: () => {
            window.location.href = "/admin";
          },
        })
      : null,
    h("button", { type: "button", class: "ghost full", text: "切换账号（仅本页）", onclick: switchAccountInThisTab }),
    h("button", { type: "button", class: "ghost full", text: text.logout, onclick: logout }),
  ]);
}

function renderUserEntry() {
  return h("section", { class: "sidebar-section user-entry-section" }, [
    h("button", {
      type: "button",
      class: "user-entry",
      onclick: () => {
        state.profileOpen = true;
        renderShell();
      },
    }, [
      avatar(state.profile?.nickname || state.user?.username || "U", state.profile?.avatar_url),
      h("span", {}, [
        h("strong", { text: state.profile?.nickname || state.user?.username || "" }),
        h("small", { text: state.profile?.signature || "查看和编辑资料" }),
      ]),
    ]),
  ]);
}

function renderProfileModal() {
  const profile = state.profile || {};
  const birthday = parseBirthday(profile.birthday);
  const nickname = h("input", { name: "nickname", value: profile.nickname || "" });
  const avatarFile = h("input", { name: "avatar_file", type: "file", accept: "image/png,image/jpeg,image/webp,image/gif" });
  const avatarUrl = h("input", { name: "avatar_url", value: profile.avatar_url || "", placeholder: "可选：粘贴图片链接" });
  const gender = selectControl(["", "男", "女", "其他", "保密"], profile.gender || "", ["未设置", "男", "女", "其他", "保密"]);
  const year = selectControl(["", ...yearOptions()], birthday.year, ["年", ...yearOptions()]);
  const month = selectControl(["", ...rangeOptions(1, 12)], birthday.month, ["月", ...rangeOptions(1, 12).map((item) => `${item}月`)]);
  const day = selectControl(["", ...rangeOptions(1, 31)], birthday.day, ["日", ...rangeOptions(1, 31).map((item) => `${item}日`)]);
  const signature = h("input", { name: "signature", value: profile.signature || "" });
  const bio = h("textarea", { name: "bio", rows: "4", maxlength: "1000", placeholder: "简单写一点关于你自己的信息" }, profile.bio || "");
  const status = h("small", { class: "save-status" });

  const syncDays = () => {
    const selectedDay = day.value;
    const maxDay = daysInMonth(year.value, month.value);
    day.replaceChildren(...["", ...rangeOptions(1, maxDay)].map((value, index) => {
      const option = h("option", { value, text: index === 0 ? "日" : `${value}日` });
      if (value === selectedDay || (Number(selectedDay) > maxDay && Number(value) === maxDay)) option.selected = true;
      return option;
    }));
  };
  year.addEventListener("change", syncDays);
  month.addEventListener("change", syncDays);
  syncDays();

  const close = () => {
    state.profileOpen = false;
    renderShell();
  };

  return h("div", { class: "profile-modal-backdrop", onclick: (event) => {
    if (event.target.classList.contains("profile-modal-backdrop")) close();
  } }, [
    h("section", { class: "profile-modal" }, [
      h("div", { class: "profile-modal-head" }, [
        h("div", { class: "profile-card-user" }, [
          avatar(profile.nickname || state.user?.username || "U", profile.avatar_url),
          h("div", {}, [
            h("h3", { text: profile.nickname || state.user?.username || "" }),
            h("small", { text: state.user?.is_guest ? guestExpiryText() : state.user?.username || "" }),
          ]),
        ]),
        h("button", { type: "button", class: "ghost compact", text: "关闭", onclick: close }),
      ]),
      state.user?.is_guest ? renderGuestConvertBox(profile) : null,
      h("form", {
        class: "profile-modal-form",
        onsubmit: async (event) => {
          event.preventDefault();
          status.textContent = "";
          try {
            let avatarValue = avatarUrl.value;
            if (avatarFile.files?.[0]) {
              status.textContent = "正在上传头像...";
              avatarValue = await uploadAvatarFile(avatarFile.files[0]);
            }
            const data = await api("/api/profile", {
              method: "PUT",
              body: JSON.stringify({
                nickname: nickname.value,
                avatar_url: avatarValue,
                gender: gender.value,
                birthday: buildBirthday(year.value, month.value, day.value),
                signature: signature.value,
                bio: bio.value,
              }),
            });
            state.profile = data.profile;
            state.profileOpen = false;
            renderShell();
          } catch (err) {
            status.textContent = err.message;
          }
        },
      }, [
        h("label", {}, [text.nickname, nickname]),
        h("label", {}, ["头像图片", avatarFile]),
        h("label", {}, ["图片链接（可选）", avatarUrl]),
        h("label", {}, ["性别", gender]),
        h("label", {}, ["生日", h("div", { class: "birthday-selects" }, [year, month, day])]),
        h("label", {}, [text.signature, signature]),
        h("label", {}, ["简介", bio]),
        h("div", { class: "actions modal-actions" }, [
          h("button", { type: "submit", text: text.save }),
          h("button", { type: "button", class: "ghost", text: "取消", onclick: close }),
        ]),
        status,
      ]),
    ]),
  ]);
}

function renderGuestConvertBox(profile) {
  const username = h("input", { name: "guest_username", autocomplete: "username", placeholder: "设置登录账号" });
  const password = h("input", {
    name: "guest_password",
    type: "password",
    minlength: "8",
    autocomplete: "new-password",
    placeholder: "至少 8 位密码",
  });
  const nickname = h("input", {
    name: "guest_nickname",
    value: profile.nickname && profile.nickname !== "游客" ? profile.nickname : "",
    autocomplete: "nickname",
    placeholder: "昵称（可选）",
  });
  const status = h("small", { class: "save-status" });
  return h("section", { class: "guest-convert" }, [
    h("div", {}, [
      h("strong", { text: "保留游客数据" }),
      h("p", { class: "muted", text: "绑定正式账号后，当前人格、聊天、记忆和头像会继续保留，不再三天后自动清除。" }),
    ]),
    h("form", {
      class: "guest-convert-form",
      onsubmit: async (event) => {
        event.preventDefault();
        status.textContent = "";
        try {
          const data = await api("/api/auth/guest/convert", {
            method: "POST",
            body: JSON.stringify({
              username: username.value,
              password: password.value,
              nickname: nickname.value,
              tab_session: tabSessionMode(),
            }),
          });
          if (tabSessionMode()) acceptAuthSession(data, true);
          state.user = data.user;
          state.profile = data.profile;
          state.profileOpen = false;
          await loadMainData();
          renderShell();
        } catch (err) {
          status.textContent = err.message;
        }
      },
    }, [
      h("label", {}, ["账号", username]),
      h("label", {}, ["密码", password]),
      h("label", {}, ["昵称", nickname]),
      h("button", { type: "submit", text: "转为正式账号" }),
      status,
    ]),
  ]);
}

function renderBrand() {
  return h("div", { class: "brand" }, [
    h("p", { class: "eyebrow", text: "Workspace" }),
    h("h1", { text: text.appName }),
    h("small", { text: text.appSubtitle }),
  ]);
}

function renderConversationSearch() {
  const input = h("input", {
    class: "conversation-search",
    type: "search",
    placeholder: "搜索聊天",
    value: state.conversationSearch,
  });
  input.addEventListener("input", () => {
    state.conversationSearch = input.value;
    renderShell();
  });
  return h("div", { class: "conversation-search-wrap" }, [
    input,
    state.conversationSearch
      ? h("button", {
          type: "button",
          class: "search-clear",
          title: "清空搜索",
          "aria-label": "清空搜索",
          text: "×",
          onclick: () => {
            state.conversationSearch = "";
            renderShell();
          },
        })
      : null,
  ]);
}

function renderChatList() {
  const list = h("div", { class: "list chat-list" });
  if (!state.personas.length) {
    list.append(h("p", { class: "empty", text: "还没有聊天对象。点右上角 + 创建一个。" }));
    return list;
  }

  const query = normalizedSearch(state.conversationSearch);
  const entries = homePersonaEntries(query);
  const groupEntries = homeGroupEntries(query);
  for (const entry of groupEntries) list.append(renderSidebarGroupItem(entry));
  for (const entry of entries) list.append(renderSidebarPersonaItem(entry));
  if (!entries.length && !groupEntries.length) {
    list.append(h("p", { class: "empty", text: "没有匹配的聊天。" }));
  }

  return list;
}

function renderArchivedConversations(items) {
  const visibleItems = items || [];
  if (!visibleItems.length) return null;
  return h("section", { class: "archive-box" }, [
    h("button", {
      type: "button",
      class: "archive-toggle",
      text: `历史聊天 ${visibleItems.length || state.archivedConversations.length}`,
      onclick: () => {
        state.showArchived = !state.showArchived;
        renderShell();
      },
    }),
    state.showArchived
      ? h("div", { class: "archive-list" }, visibleItems.map(renderArchivedConversationItem))
      : null,
  ]);
}

function renderArchivedConversationItem(item) {
  return h("div", { class: "archived-item" }, [
    avatar(item.persona_name || "忆", item.persona_avatar_url),
    h("span", {}, [
      h("strong", { text: conversationDisplayTitle(item) }),
      h("small", { text: conversationPreview(item) }),
    ]),
    h("button", {
      type: "button",
      class: "ghost compact",
      text: "移回聊天",
      onclick: async () => {
        await api(`/api/conversations/${item.id}`, {
          method: "PATCH",
          body: JSON.stringify({ status: "active" }),
        });
        state.showArchived = false;
        await loadMainData();
        renderShell();
      },
    }),
  ]);
}

function renderArchivedGroupConversations() {
  const visibleItems = state.archivedGroupConversations || [];
  if (!visibleItems.length) return null;
  return h("section", { class: "archive-box group-archive-box" }, [
    h("button", {
      type: "button",
      class: "archive-toggle",
      text: `历史群聊 ${visibleItems.length}`,
      onclick: () => {
        state.showArchivedGroups = !state.showArchivedGroups;
        renderShell();
      },
    }),
    state.showArchivedGroups
      ? h("div", { class: "archive-list" }, visibleItems.map(renderArchivedGroupConversationItem))
      : null,
  ]);
}

function renderArchivedGroupConversationItem(item) {
  return h("div", { class: "archived-item archived-group-item" }, [
    groupAvatar(item),
    h("span", {}, [
      h("strong", { text: groupConversationTitle(item) }),
      h("small", { text: groupConversationPreview(item) }),
    ]),
    h("button", {
      type: "button",
      class: "ghost compact",
      text: "移回群聊",
      onclick: async () => {
        await api(`/api/group-conversations/${item.id}`, {
          method: "PATCH",
          body: JSON.stringify({ status: "active" }),
        });
        state.showArchivedGroups = false;
        await loadMainData();
        renderShell();
      },
    }),
  ]);
}

function renderSidebarGroupItem(entry) {
  const group = entry.group;
  return h(
    "button",
    {
      type: "button",
      class: `list-item sidebar-persona-item group-list-item ${Number(state.activeGroupConversationId) === Number(group.id) && state.view === "group" ? "active" : ""}`,
      onclick: () => openGroupConversationItem(group),
    },
    [
      groupAvatar(group),
      h("span", {}, [
        h("span", { class: "conversation-title-row" }, [
          h("strong", {}, [
            entry.pinned ? h("span", { class: "pin-mark", text: "缃《" }) : null,
            groupConversationTitle(group),
          ]),
          h("small", { text: formatListTime(entry.updatedAt) }),
        ]),
        h("small", { text: groupConversationPreview(group) }),
      ]),
      Number(group.unread_count || 0) ? renderUnreadBadge(Number(group.unread_count)) : null,
    ]
  );
}

function renderConversationChatItem(item) {
  const isEditing = Number(state.editingConversationId) === Number(item.id);
  return h("div", { class: `conversation-shell ${isEditing ? "editing" : ""}` }, [
    h(
      "button",
      {
        type: "button",
        class: `list-item conversation-item ${Number(state.activeConversationId) === Number(item.id) ? "active" : ""}`,
        onclick: () => openConversationItem(item),
      },
      [
        avatar(item.persona_name || "忆", item.persona_avatar_url),
        h("span", {}, [
          h("span", { class: "conversation-title-row" }, [
            h("strong", {}, [
              isPinnedConversation(item) ? h("span", { class: "pin-mark", text: "置顶" }) : null,
              conversationDisplayTitle(item),
            ]),
            h("span", { class: "thread-meta" }, [
              h("small", { text: formatListTime(item.updated_at || item.created_at) }),
              Number(item.unread_count || 0) ? renderUnreadBadge(Number(item.unread_count)) : null,
            ]),
          ]),
          h("small", { text: conversationPreview(item) }),
        ]),
      ]
    ),
    h("button", {
      type: "button",
      class: "conversation-more",
      title: "会话设置",
      "aria-label": "会话设置",
      text: "⋯",
      onclick: (event) => {
        event.stopPropagation();
        state.editingConversationId = isEditing ? null : item.id;
        renderShell();
      },
    }),
    isEditing ? renderConversationEditor(item) : null,
  ]);
}

function renderConversationEditor(item) {
  const input = h("input", { value: item.title || item.persona_name || "聊天", maxlength: "80" });
  const status = h("small", { class: "save-status" });
  const saveTitle = async () => {
    const title = input.value.trim();
    if (!title) {
      status.textContent = "标题不能为空";
      return;
    }
    try {
      await api(`/api/conversations/${item.id}`, {
        method: "PATCH",
        body: JSON.stringify({ title }),
      });
      state.editingConversationId = null;
      await loadMainData();
      renderShell();
    } catch (err) {
      status.textContent = err.message;
    }
  };
  return h("div", { class: "conversation-editor" }, [
    input,
    h("div", { class: "conversation-editor-actions" }, [
      h("button", { type: "button", class: "compact", text: "保存", onclick: saveTitle }),
      h("button", {
        type: "button",
        class: "ghost compact",
        text: isPinnedConversation(item) ? "取消置顶" : "置顶",
        onclick: async () => {
          try {
            await api(`/api/conversations/${item.id}`, {
              method: "PATCH",
              body: JSON.stringify({ pinned: !isPinnedConversation(item) }),
            });
            state.editingConversationId = null;
            await loadMainData();
            renderShell();
          } catch (err) {
            status.textContent = err.message;
          }
        },
      }),
      h("button", {
        type: "button",
        class: "ghost compact",
        text: "移到历史",
        onclick: async () => {
          try {
            await api(`/api/conversations/${item.id}`, {
              method: "PATCH",
              body: JSON.stringify({ status: "archived" }),
            });
            if (Number(state.activeConversationId) === Number(item.id)) {
              state.activeConversationId = null;
              state.messages = [];
            }
            state.editingConversationId = null;
            if (state.threadPanelOpen && state.activePersona) {
              await loadMainData();
              await openLatestConversationForPersona(state.activePersona.id);
              state.view = "chat";
            } else {
              await loadMainData({ openLatest: true });
            }
            renderShell();
          } catch (err) {
            status.textContent = err.message;
          }
        },
      }),
      h("button", {
        type: "button",
        class: "ghost compact",
        text: "取消",
        onclick: () => {
          state.editingConversationId = null;
          renderShell();
        },
      }),
    ]),
    status,
  ]);
}

function renderSidebarPersonaItem(entry) {
  const persona = entry.persona;
  const conversation = entry.conversation;
  return h(
    "button",
    {
      type: "button",
      class: `list-item sidebar-persona-item ${Number(state.activePersona?.id) === Number(persona.id) && state.view === "chat" ? "active" : ""}`,
      onclick: () => conversation ? openConversationItem(conversation) : openPersonaItem(persona),
    },
    [
      avatar(persona.name, persona.avatar_url),
      h("span", {}, [
        h("span", { class: "conversation-title-row" }, [
          h("strong", {}, [
            entry.pinned ? h("span", { class: "pin-mark", text: "置顶" }) : null,
            persona.name,
          ]),
          h("small", { text: conversation ? formatListTime(entry.updatedAt) : "" }),
        ]),
        h("small", { text: conversation ? conversationPreview(conversation) : persona.summary || "还没开始，点开说第一句" }),
      ]),
      renderPersonaAlerts(entry, true),
    ]
  );
}

function renderHome() {
  const query = normalizedSearch(state.conversationSearch);
  const entries = homePersonaEntries(query);
  const groupEntries = homeGroupEntries(query);

  return h("section", { class: "home-screen" }, [
    h("header", { class: "home-head" }, [
      h("div", {}, [
        h("p", { class: "eyebrow", text: state.user?.is_guest ? "游客体验" : "Mnemosyne" }),
        h("h2", { text: "聊天" }),
        state.user?.is_guest ? h("small", { text: guestExpiryText() }) : null,
      ]),
      h("div", { class: "home-actions" }, [
        isAdmin()
          ? h("button", {
              type: "button",
              class: "ghost compact",
              text: text.adminConsole,
              onclick: () => {
                window.location.href = "/admin";
              },
            })
          : null,
        h("button", {
          type: "button",
          class: "ghost compact home-profile-action",
          text: "资料",
          onclick: () => {
            state.profileOpen = true;
            renderShell();
          },
        }),
        h("button", {
          type: "button",
          class: "ghost compact",
          text: "新群聊",
          disabled: state.personas.length < 2 ? "disabled" : null,
          onclick: () => {
            state.groupCreateOpen = true;
            renderShell();
          },
        }),
        state.deletedPersonas.length
          ? h("button", {
              type: "button",
              class: "ghost compact",
              text: "已删除",
              onclick: () => {
                state.deletedPanelOpen = true;
                renderShell();
              },
            })
          : null,
        h("button", {
          type: "button",
          class: "icon-btn",
          title: text.createPersona,
          "aria-label": text.createPersona,
          text: "+",
          onclick: () => {
            state.view = "forge";
            renderShell();
          },
        }),
      ]),
    ]),
    h("section", {
      class: "home-user-strip",
      role: "button",
      tabindex: "0",
      onclick: () => {
        state.profileOpen = true;
        renderShell();
      },
      onkeydown: (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          state.profileOpen = true;
          renderShell();
        }
      },
    }, [
      avatar(state.profile?.nickname || state.user?.username || "U", state.profile?.avatar_url),
      h("span", {}, [
        h("strong", { text: state.profile?.nickname || state.user?.username || "" }),
        h("small", { text: state.user?.is_guest ? guestExpiryText() : state.profile?.signature || "查看和编辑资料" }),
      ]),
      h("button", {
        type: "button",
        class: "ghost compact",
        text: "退出",
        onclick: (event) => {
          event.stopPropagation();
          logout();
        },
      }),
    ]),
    h("div", { class: "home-search" }, [renderConversationSearch()]),
    groupEntries.length
      ? h("section", { class: "home-group-section" }, [
          h("div", { class: "section-head" }, [
            h("strong", { text: "群聊" }),
            h("small", { text: "多个人格一起接话" }),
          ]),
          h("div", { class: "home-persona-list group-home-list" }, groupEntries.map(renderHomeGroupCard)),
        ])
      : null,
    renderArchivedGroupConversations(),
    h("div", { class: "home-persona-list" }, [
      ...entries.map(renderHomePersonaCard),
      !entries.length
        ? h("p", { class: "empty", text: query ? "没有匹配的聊天。" : "还没有聊天对象，点右上角 + 创建一个。" })
        : null,
    ]),
  ]);
}

function renderHomeGroupCard(entry) {
  const group = entry.group;
  return h("article", {
    class: "home-persona-card home-group-card",
    role: "button",
    tabindex: "0",
    onclick: () => openGroupConversationItem(group),
    onkeydown: (event) => {
      if (event.target !== event.currentTarget) return;
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openGroupConversationItem(group);
      }
    },
  }, [
    groupAvatar(group),
    h("span", { class: "home-card-copy" }, [
      h("span", { class: "home-card-title" }, [
        h("strong", {}, [
          entry.pinned ? h("span", { class: "pin-mark", text: "缃《" }) : null,
          groupConversationTitle(group),
        ]),
        h("small", { text: formatListTime(entry.updatedAt) }),
      ]),
      h("small", { text: groupConversationPreview(group) }),
    ]),
    Number(group.unread_count || 0) ? renderUnreadBadge(Number(group.unread_count)) : null,
  ]);
}

function renderHomePersonaCard(entry) {
  const persona = entry.persona;
  const conversation = entry.conversation;
  const archivedConversation = entry.archivedConversation;
  const openCard = () => conversation ? openConversationItem(conversation) : openPersonaItem(persona);
  return h("article", {
    class: "home-persona-card",
    role: "button",
    tabindex: "0",
    onclick: openCard,
    onkeydown: (event) => {
      if (event.target !== event.currentTarget) return;
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openCard();
      }
    },
  }, [
    avatar(persona.name, persona.avatar_url),
    h("span", { class: "home-card-copy" }, [
      h("span", { class: "home-card-title" }, [
        h("strong", {}, [
          entry.pinned ? h("span", { class: "pin-mark", text: "置顶" }) : null,
          persona.name,
        ]),
        h("small", { text: conversation || archivedConversation ? formatListTime(entry.updatedAt) : "未开聊" }),
      ]),
      conversation
        ? h("small", { text: conversationPreview(conversation) })
        : archivedConversation
          ? h("small", { text: `历史：${conversationPreview(archivedConversation)}` })
          : renderInlinePersonaMeta(persona) || h("small", { text: persona.summary || "点开说第一句" }),
    ]),
    renderPersonaAlerts(entry, false, persona),
  ]);
}

function renderPersonaAlerts(entry, compact = false, growthActionPersona = null) {
  const notices = [];
  if (entry.unreadCount) notices.push(renderUnreadBadge(entry.unreadCount));
  if (entry.persona?.growth_notice) {
    notices.push(
      growthActionPersona
        ? h("button", {
            type: "button",
            class: `growth-notice-badge action ${compact ? "compact" : ""}`.trim(),
            title: "直接查看相处痕迹",
            "aria-label": "直接查看相处痕迹",
            text: compact ? "变化" : "查看变化",
            onclick: (event) => openPersonaGrowthNotice(event, growthActionPersona),
          })
        : h("span", {
            class: `growth-notice-badge ${compact ? "compact" : ""}`.trim(),
            title: "查看资料中的相处痕迹",
            text: compact ? "变化" : "有变化",
          })
    );
  }
  if (entry.persona?.growth_action?.kind === "preference_retry") {
    notices.push(
      growthActionPersona
        ? h("button", {
            type: "button",
            class: `preference-action-badge action ${compact ? "compact" : ""}`.trim(),
            title: "打开相处痕迹重新确认偏好",
            "aria-label": "打开相处痕迹重新确认偏好",
            text: compact ? "待确认" : "偏好需确认",
            onclick: (event) => openPersonaGrowthNotice(event, growthActionPersona),
          })
        : h("span", {
            class: `preference-action-badge ${compact ? "compact" : ""}`.trim(),
            title: "到相处痕迹中重新确认偏好",
            text: compact ? "待确认" : "偏好需确认",
          })
    );
  }
  return notices.length ? h("span", { class: "persona-alert-stack" }, notices) : null;
}

function homePersonaEntries(query) {
  const entries = [];
  for (const persona of state.personas) {
    const threads = state.conversations.filter((item) => Number(item.persona_id) === Number(persona.id));
    const archived = state.archivedConversations.filter((item) => Number(item.persona_id) === Number(persona.id));
    if (query && !matchesPersonaSearch(persona, query) && !threads.some((item) => matchesConversationSearch(item, query)) && !archived.some((item) => matchesConversationSearch(item, query))) {
      continue;
    }
    const recentThreads = [...threads].sort((left, right) => Number(right.updated_at || 0) - Number(left.updated_at || 0));
    const latest = recentThreads.find((item) => Number(item.unread_count || 0) > 0) || recentThreads[0] || null;
    const latestArchived = [...archived].sort((left, right) => Number(right.updated_at || 0) - Number(left.updated_at || 0))[0] || null;
    entries.push({
      persona,
      conversation: latest,
      archivedConversation: latestArchived,
      unreadCount: threads.reduce((total, item) => total + Number(item.unread_count || 0), 0),
      pinned: threads.some(isPinnedConversation),
      pinnedAt: Math.max(0, ...threads.map((item) => Number(item.pinned_at || 0))),
      updatedAt: Number(latest?.updated_at || latestArchived?.updated_at || persona.updated_at || persona.created_at || 0),
    });
  }
  return entries.sort((left, right) => right.pinnedAt - left.pinnedAt || right.updatedAt - left.updatedAt);
}

function homeGroupEntries(query) {
  return state.groupConversations
    .filter((group) => matchesGroupSearch(group, query))
    .map((group) => ({
      group,
      pinned: isPinnedConversation(group),
      pinnedAt: Number(group.pinned_at || 0),
      updatedAt: Number(group.updated_at || group.created_at || 0),
    }))
    .sort((left, right) => right.pinnedAt - left.pinnedAt || right.updatedAt - left.updatedAt);
}

function renderUnreadBadge(count) {
  const value = count > 99 ? "99+" : String(count);
  return h("span", { class: `unread-badge ${count > 9 ? "wide" : ""}`, text: value });
}

function guestExpiryText() {
  const expiresAt = Number(state.user?.guest_expires_at || 0);
  if (!expiresAt) return "游客模式，三天后自动清除数据";
  const seconds = Math.max(0, expiresAt - nowSeconds());
  const days = Math.ceil(seconds / 86400);
  if (days > 1) return `游客模式，约 ${days} 天后自动清除数据`;
  const hours = Math.ceil(seconds / 3600);
  return `游客模式，约 ${Math.max(1, hours)} 小时后自动清除数据`;
}

function renderMain() {
  if (state.view === "home") {
    return renderHome();
  }

  if (state.view === "forge" || !state.activePersona) {
    if (state.view === "group" && state.activeGroupConversation) {
      return renderGroupChat();
    }
    return renderWizard();
  }

  return h("section", { class: "workspace" }, [
    h("section", { class: "chat-panel" }, [
      h("header", { class: "chat-header" }, [
        h("button", {
          type: "button",
          class: "ghost compact home-back-btn",
          text: isMobileShell() ? "返回" : "返回主界面",
          onclick: returnToHome,
        }),
        h("button", {
          type: "button",
          class: "avatar-button",
          title: "查看资料",
          "aria-label": "查看资料",
          onclick: openPersonaPanel,
        }, [avatar(state.activePersona.name, state.activePersona.avatar_url)]),
        h("div", { class: "chat-header-copy" }, [
          h("h2", { text: state.activePersona.name }),
          renderPersonaStatusLine(state.activePersona),
          h("p", { text: state.activePersona.summary || "人格会在对话和记忆里继续生长。" }),
        ]),
        h("div", { class: "chat-header-actions" }, [
          h("button", {
            type: "button",
            class: "ghost compact chat-profile-action",
            text: "资料",
            onclick: openPersonaPanel,
          }),
          h("button", {
            type: "button",
            class: "ghost compact chat-new-action",
            text: "记录",
            title: "聊天记录",
            "aria-label": "聊天记录",
            onclick: () => {
              state.threadPanelOpen = true;
              state.editingConversationId = null;
              renderShell();
            },
          }),
        ]),
      ]),
      h("div", { id: "chat-log", class: "chat-log" }, state.messages.length ? renderMessageList(state.messages) : [renderChatEmpty()]),
      renderComposer(),
    ]),
  ]);
}

function renderGroupChat() {
  const group = state.activeGroupConversation || {};
  return h("section", { class: "workspace group-workspace" }, [
    h("section", { class: "chat-panel" }, [
      h("header", { class: "chat-header group-chat-header" }, [
        h("button", {
          type: "button",
          class: "ghost compact home-back-btn",
          text: isMobileShell() ? "返回" : "返回主界面",
          onclick: returnToHome,
        }),
        groupAvatar(group),
        h("div", { class: "chat-header-copy" }, [
          h("h2", { text: groupConversationTitle(group) }),
          h("div", { class: "persona-status-line" }, (group.members || []).slice(0, 6).map((member) => (
            h("span", { class: "status-pill", text: member.display_name || member.name || "TA" })
          ))),
          h("p", { text: "群聊模式：大家会视情况接话，也可以保持沉默。" }),
        ]),
        h("div", { class: "chat-header-actions" }, [
          h("button", {
            type: "button",
            class: "ghost compact chat-new-action",
            text: "设置",
            onclick: () => {
              state.groupSettingsOpen = true;
              renderShell();
            },
          }),
          h("button", {
            type: "button",
            class: "ghost compact chat-new-action",
            text: "归档",
            onclick: async () => {
              await api(`/api/group-conversations/${group.id}`, {
                method: "PATCH",
                body: JSON.stringify({ status: "archived" }),
              });
              state.view = "home";
              state.activeGroupConversationId = null;
              state.activeGroupConversation = null;
              state.groupMessages = [];
              state.groupSettingsOpen = false;
              await loadMainData();
              renderShell();
            },
          }),
        ]),
      ]),
      h("div", { id: "chat-log", class: "chat-log group-chat-log" }, state.groupMessages.length ? renderGroupMessageList(state.groupMessages) : [renderGroupChatEmpty()]),
      renderComposer(),
    ]),
    state.groupSettingsOpen ? renderGroupSettingsModal(group) : null,
  ]);
}

function renderGroupSettingsModal(group) {
  const input = h("input", { value: groupConversationTitle(group), maxlength: "80" });
  const status = h("small", { class: "save-status" });
  const activeMembers = group.members || [];
  const activeMemberIds = new Set(activeMembers.map((member) => Number(member.persona_id)));
  const candidatePersonas = (state.personas || []).filter((persona) => !activeMemberIds.has(Number(persona.id)));
  const memberSelect = h("select", {}, candidatePersonas.map((persona) => (
    h("option", { value: persona.id, text: persona.name || "TA" })
  )));
  const autoTurnToggle = h("input", {
    type: "checkbox",
    checked: groupAutoTurnEnabled(group.id) ? "checked" : null,
    onchange: (event) => {
      setGroupAutoTurnEnabled(group.id, event.target.checked);
      status.textContent = event.target.checked ? "自主续聊已开启" : "自主续聊已关闭";
    },
  });
  const applyGroupUpdate = async (data) => {
    state.activeGroupConversation = data.group_conversation;
    state.activeGroupConversationId = data.group_conversation?.id || state.activeGroupConversationId;
    await loadMainData();
    renderShell();
  };
  const close = () => {
    state.groupSettingsOpen = false;
    renderShell();
  };
  const saveTitle = async () => {
    const title = input.value.trim();
    if (!title) {
      status.textContent = "群名不能为空";
      return;
    }
    try {
      await api(`/api/group-conversations/${group.id}`, {
        method: "PATCH",
        body: JSON.stringify({ title }),
      });
      await loadMainData();
      state.groupSettingsOpen = false;
      renderShell();
    } catch (err) {
      status.textContent = err.message;
    }
  };
  const archiveGroup = async () => {
    try {
      await api(`/api/group-conversations/${group.id}`, {
        method: "PATCH",
        body: JSON.stringify({ status: "archived" }),
      });
      state.view = "home";
      state.activeGroupConversationId = null;
      state.activeGroupConversation = null;
      state.groupMessages = [];
      state.groupSettingsOpen = false;
      await loadMainData();
      renderShell();
    } catch (err) {
      status.textContent = err.message;
    }
  };
  const addMember = async () => {
    const personaId = Number(memberSelect.value);
    if (!personaId) return;
    try {
      status.textContent = "正在加入...";
      const data = await api(`/api/group-conversations/${group.id}/members`, {
        method: "POST",
        body: JSON.stringify({ persona_id: personaId }),
      });
      await applyGroupUpdate(data);
    } catch (err) {
      status.textContent = err.message;
    }
  };
  const removeMember = async (personaId) => {
    try {
      status.textContent = "正在移除...";
      const data = await api(`/api/group-conversations/${group.id}/members/${personaId}`, {
        method: "DELETE",
      });
      await applyGroupUpdate(data);
    } catch (err) {
      status.textContent = err.message;
    }
  };
  return h("div", { class: "profile-modal-backdrop", onclick: (event) => {
    if (event.target.classList.contains("profile-modal-backdrop")) close();
  } }, [
    h("section", { class: "profile-modal group-settings-modal" }, [
      h("div", { class: "profile-modal-head" }, [
        h("h3", { text: "群设置" }),
        h("button", { type: "button", class: "ghost compact", text: "关闭", onclick: close }),
      ]),
      h("div", { class: "group-settings-row" }, [
        h("label", {}, [
          h("span", { text: "群名" }),
          input,
        ]),
        h("button", { type: "button", class: "compact", text: "保存", onclick: saveTitle }),
        h("button", {
          type: "button",
          class: "ghost compact",
          text: isPinnedConversation(group) ? "取消置顶" : "置顶",
          onclick: async () => {
            try {
              await api(`/api/group-conversations/${group.id}`, {
                method: "PATCH",
                body: JSON.stringify({ pinned: !isPinnedConversation(group) }),
              });
              await loadMainData();
              renderShell();
            } catch (err) {
              status.textContent = err.message;
            }
          },
        }),
        h("button", { type: "button", class: "ghost compact danger-soft", text: "移到历史", onclick: archiveGroup }),
      ]),
      h("div", { class: "group-settings-members" }, [
        h("strong", { text: "群成员" }),
        h("div", { class: "group-member-strip" }, activeMembers.map((member) => (
          h("span", { class: "group-member-pill" }, [
            avatar(member.display_name || member.name || "TA", member.avatar_url),
            h("span", { text: member.display_name || member.name || "TA" }),
            h("button", {
              type: "button",
              class: "group-member-remove",
              title: activeMembers.length <= 2 ? "群聊至少保留两位成员" : "移出群聊",
              text: "×",
              disabled: activeMembers.length <= 2 ? "disabled" : null,
              onclick: () => removeMember(member.persona_id),
            }),
          ])
        ))),
      ]),
      h("div", { class: "group-member-add" }, [
        candidatePersonas.length
          ? memberSelect
          : h("small", { text: "没有可加入的其他人格。" }),
        h("button", {
          type: "button",
          class: "ghost compact",
          text: "加入群聊",
          disabled: candidatePersonas.length ? null : "disabled",
          onclick: addMember,
        }),
      ]),
      h("label", { class: "group-auto-toggle" }, [
        autoTurnToggle,
        h("span", { text: "自主续聊" }),
      ]),
      status,
    ]),
  ]);
}

function renderGroupChatEmpty() {
  const group = state.activeGroupConversation || {};
  return h("div", { class: "chat-empty" }, [
    groupAvatar(group),
    h("h3", { text: groupConversationTitle(group) }),
    h("p", { text: "发一句话，群里的成员会自己判断谁来接。" }),
    h("div", { class: "chat-empty-actions" }, [
      chatStarterButton("你们一起聊聊看。"),
      chatStarterButton("我今天有点无聊。"),
      chatStarterButton("给我一个新的话题。"),
    ]),
  ]);
}

function renderPersonaModal() {
  const close = () => {
    state.personaPanelOpen = false;
    state.editingPersona = false;
    renderShell();
  };
  return h("div", { class: "profile-modal-backdrop", onclick: (event) => {
    if (event.target.classList.contains("profile-modal-backdrop")) close();
  } }, [
    h("section", { class: "profile-modal persona-modal" }, [
      h("div", { class: "profile-modal-head" }, [
        h("strong", { text: "资料" }),
        h("button", { type: "button", class: "ghost compact", text: "关闭", onclick: close }),
      ]),
      renderPersonaRail(),
    ]),
  ]);
}

function renderThreadModal() {
  const persona = state.activePersona || {};
  const activeThreads = state.conversations.filter((item) => Number(item.persona_id) === Number(persona.id));
  const archivedThreads = state.archivedConversations.filter((item) => Number(item.persona_id) === Number(persona.id));
  const close = () => {
    state.threadPanelOpen = false;
    state.editingConversationId = null;
    renderShell();
  };
  return h("div", { class: "profile-modal-backdrop", onclick: (event) => {
    if (event.target.classList.contains("profile-modal-backdrop")) close();
  } }, [
    h("section", { class: "profile-modal thread-modal" }, [
      h("div", { class: "profile-modal-head" }, [
        h("strong", { text: `${persona.name || "TA"} 的聊天记录` }),
        h("button", { type: "button", class: "ghost compact", text: "关闭", onclick: close }),
      ]),
      h("button", {
        type: "button",
        class: "ghost new-thread-action",
        text: text.newChat,
        onclick: () => {
          state.activeConversationId = null;
          state.messages = [];
          state.threadPanelOpen = false;
          state.editingConversationId = null;
          state.focusComposer = true;
          renderShell();
        },
      }),
      h("div", { class: "thread-list" }, [
        ...activeThreads.map(renderConversationChatItem),
        !activeThreads.length ? h("p", { class: "empty", text: "还没有正在进行的聊天。" }) : null,
        archivedThreads.length ? renderArchivedConversations(archivedThreads) : null,
      ]),
    ]),
  ]);
}

function renderGroupCreateModal() {
  const selected = new Set();
  const titleInput = h("input", { maxlength: "80", placeholder: "群聊标题（可选）" });
  const status = h("small", { class: "save-status" });
  const createButton = h("button", { type: "submit", text: "创建群聊", disabled: "disabled" });
  const close = () => {
    state.groupCreateOpen = false;
    renderShell();
  };
  const updateCreateState = () => {
    createButton.disabled = selected.size < 2;
    status.textContent = selected.size < 2 ? "至少选择 2 个人格。" : `已选择 ${selected.size} 个成员。`;
  };
  const memberButtons = state.personas.map((persona) => {
    const button = h("button", {
      type: "button",
      class: "group-member-choice",
      onclick: () => {
        const id = Number(persona.id);
        if (selected.has(id)) selected.delete(id);
        else if (selected.size < 6) selected.add(id);
        button.classList.toggle("active", selected.has(id));
        updateCreateState();
      },
    }, [
      avatar(persona.name, persona.avatar_url),
      h("span", {}, [
        h("strong", { text: persona.name }),
        h("small", { text: persona.summary || persona.speaking_style || "" }),
      ]),
    ]);
    return button;
  });
  updateCreateState();
  return h("div", { class: "profile-modal-backdrop", onclick: (event) => {
    if (event.target.classList.contains("profile-modal-backdrop")) close();
  } }, [
    h("section", { class: "profile-modal group-create-modal" }, [
      h("div", { class: "profile-modal-head" }, [
        h("strong", { text: "新建群聊" }),
        h("button", { type: "button", class: "ghost compact", text: "关闭", onclick: close }),
      ]),
      h("p", { class: "muted", text: "选择 2-6 个人格。群里不是每个人都会每轮说话，会由调度器判断谁接。" }),
      h("form", {
        class: "group-create-form",
        onsubmit: async (event) => {
          event.preventDefault();
          if (selected.size < 2) return;
          createButton.disabled = true;
          status.textContent = "正在创建群聊...";
          try {
            const data = await api("/api/group-conversations", {
              method: "POST",
              body: JSON.stringify({
                title: titleInput.value.trim() || null,
                persona_ids: [...selected],
              }),
            });
            state.groupCreateOpen = false;
            await loadMainData();
            await openGroupConversationItem(data.group_conversation);
          } catch (err) {
            status.textContent = err.message;
            createButton.disabled = false;
          }
        },
      }, [
        h("label", {}, ["标题", titleInput]),
        h("div", { class: "group-member-grid" }, memberButtons),
        h("div", { class: "actions modal-actions" }, [
          createButton,
          h("button", { type: "button", class: "ghost", text: "取消", onclick: close }),
        ]),
        status,
      ]),
    ]),
  ]);
}

function renderDeletedPersonasModal() {
  const close = () => {
    state.deletedPanelOpen = false;
    renderShell();
  };
  return h("div", { class: "profile-modal-backdrop", onclick: (event) => {
    if (event.target.classList.contains("profile-modal-backdrop")) close();
  } }, [
    h("section", { class: "profile-modal deleted-personas-modal" }, [
      h("div", { class: "profile-modal-head" }, [
        h("strong", { text: "已删除的人格" }),
        h("button", { type: "button", class: "ghost compact", text: "关闭", onclick: close }),
      ]),
      h("p", { class: "muted deleted-personas-note", text: "恢复后，TA 和保留的聊天记录会重新回到主界面。" }),
      h("div", { class: "deleted-personas-list" }, state.deletedPersonas.length
        ? state.deletedPersonas.map((persona) => renderDeletedPersonaItem(persona))
        : [h("p", { class: "empty", text: "没有已删除的人格。" })]),
    ]),
  ]);
}

function renderDeletedPersonaItem(persona) {
  const status = h("small", { class: "save-status" });
  return h("div", { class: "deleted-persona-item" }, [
    avatar(persona.name || "忆", persona.avatar_url),
    h("span", {}, [
      h("strong", { text: persona.name || "未命名" }),
      h("small", { text: persona.summary || persona.relationship || "资料仍被保留" }),
      status,
    ]),
    h("button", {
      type: "button",
      class: "ghost compact",
      text: "恢复",
      onclick: async () => {
        status.textContent = "";
        try {
          await api(`/api/personas/${persona.id}/restore`, { method: "POST" });
          await loadMainData();
          state.deletedPanelOpen = false;
          state.view = "home";
          renderShell();
        } catch (err) {
          status.textContent = err.message;
        }
      },
    }),
  ]);
}

function renderPersonaRail() {
  const persona = state.activePersona || {};
  if (state.editingPersona) {
    return h("div", { class: "profile-rail" }, [renderPersonaEditForm(persona)]);
  }
  return h("div", { class: "profile-rail" }, [
    h("section", { class: "rail-section" }, [
      h("div", { class: "section-head" }, [
        h("p", { class: "eyebrow", text: "联系人资料" }),
        h("button", {
          type: "button",
          class: "ghost compact",
          text: "编辑",
          onclick: () => {
            state.editingPersona = true;
            renderShell();
          },
        }),
      ]),
      h("div", { class: "rail-identity" }, [
        avatar(persona.name || "忆", persona.avatar_url),
        h("div", {}, [
          h("h3", { text: persona.name || "未命名" }),
          h("small", { text: persona.relationship || "关系尚未明确" }),
        ]),
      ]), 
      detailLine("说话方式", persona.speaking_style || "会在对话中逐步稳定"),
      detailLine("摘要", persona.summary || "还在形成中"),
      renderPersonaExpressionPreference(persona),
      renderPersonaGrowth(persona),
      h("details", { class: "persona-more-details" }, [
        h("summary", { text: "更多资料" }),
        detailLine("人格版本", `v${persona.version || 1}`),
        detailLine("外貌参考", persona.appearance_description || "暂未确定"),
        detailLine("期望形象", persona.desired_image || "暂未确定"),
        detailLine("心理适配", persona.psychological_fit_notes || "先从稳定倾听开始，再根据长期对话校准。"),
        detailLine("成长方向", persona.growth_notes || profileList(persona.psychological_profile?.growth_direction) || "由聊天、记忆和关系状态逐步推动。"),
      ]),
    ]),
  ]);
}

function expressionPreferenceEnabled(persona) {
  return persona?.expression_preference?.enabled !== false;
}

function expressionPreferenceMode(persona) {
  if (!expressionPreferenceEnabled(persona)) return "off";
  return persona?.expression_preference?.mode || "normal";
}

function applyPersonaExpressionPreference(personaId, preference) {
  const update = (persona) => (
    Number(persona.id) === Number(personaId)
      ? { ...persona, expression_preference: preference }
      : persona
  );
  state.personas = state.personas.map(update);
  if (state.activePersona && Number(state.activePersona.id) === Number(personaId)) {
    state.activePersona = update(state.activePersona);
  }
}

function applyPersonaUpdate(updatedPersona) {
  if (!updatedPersona?.id) return;
  state.activePersona = state.activePersona && Number(state.activePersona.id) === Number(updatedPersona.id)
    ? updatedPersona
    : state.activePersona;
  state.personas = state.personas.map((item) => (
    Number(item.id) === Number(updatedPersona.id) ? updatedPersona : item
  ));
  state.conversations = state.conversations.map((item) => (
    Number(item.persona_id) === Number(updatedPersona.id)
      ? { ...item, persona_name: updatedPersona.name, persona_avatar_url: updatedPersona.avatar_url }
      : item
  ));
  state.archivedConversations = state.archivedConversations.map((item) => (
    Number(item.persona_id) === Number(updatedPersona.id)
      ? { ...item, persona_name: updatedPersona.name, persona_avatar_url: updatedPersona.avatar_url }
      : item
  ));
}

function renderPersonaExpressionPreference(persona) {
  const mode = expressionPreferenceMode(persona);
  const enabled = mode !== "off";
  const status = h("small", { class: "save-status" });
  const modeCopy = {
    off: "已关闭：聊天回复不会再显示轻表达标签。",
    subtle: "克制显示：只在明显需要承接情绪时出现。",
    normal: "正常显示：合适时显示微笑、轻声、点头这类克制提示。",
  };
  const options = [
    ["off", "关闭"],
    ["subtle", "克制"],
    ["normal", "正常"],
  ];
  return h("section", { class: `persona-expression-card ${enabled ? "enabled" : "disabled"}` }, [
    h("div", {}, [
      h("strong", { text: "轻表达" }),
      h("p", {
        text: modeCopy[mode] || modeCopy.normal,
      }),
    ]),
    h("div", { class: "segmented-control expression-mode-control" }, options.map(([value, label]) => h("button", {
      type: "button",
      class: value === mode ? "active" : "",
      text: label,
      disabled: value === mode ? "disabled" : null,
      onclick: async (event) => {
        const button = event.currentTarget;
        button.disabled = true;
        status.textContent = "正在保存...";
        try {
          const data = await api(`/api/personas/${persona.id}/expression-preference`, {
            method: "PATCH",
            body: JSON.stringify({ mode: value }),
          });
          applyPersonaExpressionPreference(persona.id, data.expression_preference || {
            enabled: value !== "off",
            mode: value,
            explicit: true,
          });
          renderShell();
        } catch (err) {
          button.disabled = false;
          status.textContent = err.message;
        }
      },
    }))),
    status,
  ]);
}

function renderPersonaGrowth(persona) {
  const personaId = Number(persona.id);
  const growth = state.personaGrowth[personaId];
  const loading = Number(state.loadingGrowthPersonaId) === personaId && !growth;
  const signals = Array.isArray(growth?.signals) ? growth.signals : [];
  const latestChange = growth?.latest_reviewed_change;
  const reviewedChanges = Array.isArray(growth?.reviewed_changes) ? growth.reviewed_changes : [];
  const preferenceRequests = Array.isArray(growth?.preference_requests) ? growth.preference_requests : [];
  return h("section", { class: "persona-growth-glimpse" }, [
    h("div", { class: "persona-growth-head" }, [
      h("strong", { text: "相处痕迹" }),
      growth?.version ? h("small", { text: `v${growth.version}` }) : null,
    ]),
    h("p", { class: "persona-growth-title", text: loading ? "正在整理最近的相处变化..." : growth?.headline || "你们的相处还在慢慢形成" }),
    !loading && signals.length
      ? h("div", { class: "persona-growth-signals" }, signals.map((signal) => h("div", { class: `persona-growth-signal ${signal.kind || ""}` }, [
        h("div", { class: "persona-growth-signal-head" }, [
          h("span", { text: signal.title || "相处变化" }),
          signal.created_at ? h("small", { text: `确认于 ${formatListTime(signal.created_at)}` }) : null,
        ]),
        h("p", { text: signal.text || "" }),
      ])))
      : null,
    latestChange ? renderGrowthFeedback(personaId, latestChange, growth?.feedback_error) : null,
    renderPreferenceRequest(personaId, preferenceRequests, growth?.request_notice, growth?.request_error),
    reviewedChanges.length ? renderReviewedChangeHistory(personaId, reviewedChanges) : null,
    h("p", {
      class: "persona-growth-hint",
      text: "想调整相处方式，可以直接说“回复短一点”或“少追问”；明确说“以后你就是我的女朋友”或“我以后叫你小舟”也会直接更新并留记录。",
    }),
  ]);
}

function renderPreferenceRequest(personaId, requests, notice = "", errorText = "") {
  const editable = requests.find((request) => (
    request.status === "active_guidance" && request.can_withdraw && request.origin === "direct_entry"
  ));
  const input = h("textarea", {
    rows: "2",
    maxlength: "500",
    placeholder: "例如：难过时先陪我一会儿，不要马上分析原因",
  }, editable?.detail || "");
  const statusText = {
    active_guidance: "当前生效",
    waiting_review: "已转为自动适配",
    confirmed: "已形成变化",
    not_applied: "本次未形成变化",
    needs_review_again: "已转为自动适配",
    withdrawn: "已撤回",
    stopped_in_chat: "已在聊天中停止",
    superseded: "已被更新偏好替代",
    recorded: "已记下",
  };
  return h("section", { class: "persona-growth-request" }, [
    h("strong", { text: "直接告诉 TA 怎么陪你" }),
    h("p", { text: "你写下的主动偏好会立即影响 TA 的回应方式；与变化反馈形成的补充指导可同时生效，也可分别停止。" }),
    input,
    h("button", {
      type: "button",
      class: "ghost compact",
      text: editable ? "更新当前偏好" : "让 TA 按这样回应",
      onclick: () => submitPreferenceRequest(personaId, input.value),
    }),
    notice ? h("small", { text: notice }) : null,
    errorText ? h("small", { class: "error", text: errorText }) : null,
    requests.length ? h("details", { class: "persona-growth-request-history" }, [
      h("summary", { text: `我提交过的偏好（${requests.length}）` }),
      h("div", {}, requests.map((request) => h("article", {}, [
        h("div", {}, [
          h("small", { text: formatListTime(request.updated_at || request.created_at) }),
          h("span", { text: statusText[request.status] || statusText.recorded }),
          request.origin === "growth_feedback"
            ? h("span", { text: `来自 v${request.source_reviewed_version || "?"} 的调整反馈` })
            : request.origin === "chat_feedback"
              ? h("span", { text: "聊天中明确提出" })
              : h("span", { text: "主动设置" }),
        ]),
        h("p", { text: request.detail }),
        request.deactivation_reason ? h("small", { text: request.deactivation_reason }) : null,
        request.result ? h("section", { class: "persona-growth-request-result" }, [
          h("strong", { text: `已在 v${request.result.version} 形成变化` }),
          h("p", { text: (request.result.highlights || []).join("；") || "相处方式完成了一次轻微调整" }),
        ]) : null,
        request.can_retry ? h("button", {
          type: "button",
          class: "ghost compact",
          text: "重新启用",
          onclick: () => retryPreferenceRequest(personaId, request.id),
        }) : null,
        request.can_withdraw ? h("button", {
          type: "button",
          class: "ghost compact",
          text: "停止这条偏好",
          onclick: () => withdrawPreferenceRequest(personaId, request.id),
        }) : null,
      ]))),
    ]) : null,
  ]);
}

function renderReviewedChangeHistory(personaId, changes) {
  return h("details", { class: "persona-growth-history" }, [
    h("summary", { text: `已确认变化记录（${changes.length}）` }),
    h("div", { class: "persona-growth-history-list" }, changes.map((change) => h("article", { class: "persona-growth-history-item" }, [
      h("div", { class: "persona-growth-history-head" }, [
        h("strong", { text: `v${change.version}` }),
        change.created_at ? h("small", { text: formatListTime(change.created_at) }) : null,
        Number(change.previous_version || 0) ? h("button", {
          type: "button",
          class: "ghost compact",
          text: `恢复 v${change.previous_version}`,
          onclick: () => restorePersonaVersion(personaId, change.previous_version),
        }) : null,
      ]),
      h("p", { text: (change.highlights || []).join("；") || "相处方式完成了一次轻微调整" }),
      change.feedback?.reaction
        ? h("small", {
            text: change.feedback.reaction === "helpful"
              ? "你的反馈：这样更合适"
              : `你的反馈：还想调整 · ${change.feedback.followup_status === "completed"
                ? `已自动采用${change.feedback.followed_up_at ? `（${formatListTime(change.feedback.followed_up_at)}）` : ""}`
                : "写下具体方式即可自动调整"}`,
          })
        : null,
    ]))),
  ]);
}

async function submitPreferenceRequest(personaId, detail) {
  const value = String(detail || "").trim();
  const growth = state.personaGrowth[personaId] || {};
  if (!value) {
    state.personaGrowth[personaId] = { ...growth, request_error: "请先写下希望调整的相处方式。" };
    renderShell();
    return;
  }
  try {
    await api(`/api/personas/${personaId}/growth/requests`, {
      method: "POST",
      body: JSON.stringify({ detail: value }),
    });
    const data = await api(`/api/personas/${personaId}/growth`);
    state.personaGrowth[personaId] = {
      ...data.growth,
      request_notice: "已生效，TA 接下来的回应会参考这条偏好。",
      request_error: "",
    };
  } catch (err) {
    state.personaGrowth[personaId] = { ...growth, request_error: err.message, request_notice: "" };
  }
  renderShell();
}

async function withdrawPreferenceRequest(personaId, requestId) {
  const growth = state.personaGrowth[personaId] || {};
  try {
    await api(`/api/personas/${personaId}/growth/requests/${requestId}/withdraw`, { method: "POST" });
    const data = await api(`/api/personas/${personaId}/growth`);
    state.personaGrowth[personaId] = {
      ...data.growth,
      request_notice: "已停止，接下来的回应不再使用这条偏好。",
      request_error: "",
    };
    clearGrowthAction(personaId);
  } catch (err) {
    state.personaGrowth[personaId] = { ...growth, request_error: err.message, request_notice: "" };
  }
  renderShell();
}

async function restorePersonaVersion(personaId, version) {
  const growth = state.personaGrowth[personaId] || {};
  try {
    const restored = await api(`/api/personas/${personaId}/versions/${version}/restore`, {
      method: "POST",
      body: JSON.stringify({ note: "从成长历史恢复" }),
    });
    if (restored.persona) {
      applyPersonaUpdate(restored.persona);
    }
    const data = await api(`/api/personas/${personaId}/growth`);
    state.personaGrowth[personaId] = {
      ...data.growth,
      request_notice: `已恢复到 v${version} 的状态，并保存为 v${restored.version}`,
      request_error: "",
    };
  } catch (err) {
    state.personaGrowth[personaId] = { ...growth, request_error: err.message, request_notice: "" };
  }
  renderShell();
}

async function retryPreferenceRequest(personaId, requestId) {
  const growth = state.personaGrowth[personaId] || {};
  try {
    await api(`/api/personas/${personaId}/growth/requests/${requestId}/retry`, { method: "POST" });
    const data = await api(`/api/personas/${personaId}/growth`);
    state.personaGrowth[personaId] = {
      ...data.growth,
      request_notice: "已重新启用，TA 接下来的回应会参考这条偏好。",
      request_error: "",
    };
    clearGrowthAction(personaId);
  } catch (err) {
    state.personaGrowth[personaId] = { ...growth, request_error: err.message, request_notice: "" };
  }
  renderShell();
}

function renderGrowthFeedback(personaId, latestChange, errorText = "") {
  const reaction = latestChange.feedback?.reaction || "";
  const followupCompleted = reaction === "needs_adjustment"
    && latestChange.feedback?.followup_status === "completed";
  const detailInput = reaction === "needs_adjustment"
    ? h("textarea", {
        rows: "2",
        maxlength: "500",
        placeholder: "例如：还是太爱追问，或者安慰时说得太满了",
      }, latestChange.feedback?.detail_text || "")
    : null;
  const message = reaction === "helpful"
    ? "已记下：这次变化更适合你。"
    : reaction === "needs_adjustment"
      ? followupCompleted
        ? `这条反馈已于 ${formatListTime(latestChange.feedback.followed_up_at)} 自动加入当前回应方式；继续补充会即时更新。`
        : latestChange.feedback?.detail_text
          ? "补充已自动加入当前回应方式。"
          : "已记下。写出具体想法后，会自动调整接下来的回应方式。"
      : "";
  return h("section", { class: "persona-growth-feedback" }, [
    h("p", { text: "这次确认后的变化，感觉怎么样？" }),
    h("div", { class: "persona-growth-feedback-actions" }, [
      h("button", {
        type: "button",
        class: `ghost compact ${reaction === "helpful" ? "selected" : ""}`.trim(),
        text: "这样更合适",
        onclick: () => submitGrowthFeedback(personaId, "helpful"),
      }),
      h("button", {
        type: "button",
        class: `ghost compact ${reaction === "needs_adjustment" ? "selected" : ""}`.trim(),
        text: "还想调整",
        onclick: reaction === "needs_adjustment" ? null : () => submitGrowthFeedback(personaId, "needs_adjustment"),
      }),
    ]),
    detailInput ? h("label", { class: "persona-growth-feedback-detail" }, [
      h("span", { text: "具体想调整什么（可选）" }),
      detailInput,
      h("button", {
        type: "button",
        class: "ghost compact",
        text: followupCompleted ? "补充并重新提交" : "保存补充",
        onclick: () => submitGrowthFeedback(personaId, "needs_adjustment", detailInput.value),
      }),
    ]) : null,
    message ? h("small", { text: message }) : null,
    errorText ? h("small", { class: "error", text: errorText }) : null,
  ]);
}

async function submitGrowthFeedback(personaId, reaction, detail = "") {
  const growth = state.personaGrowth[personaId];
  if (!growth?.latest_reviewed_change) return;
  try {
    const data = await api(`/api/personas/${personaId}/growth/feedback`, {
      method: "POST",
      body: JSON.stringify({ reaction, detail }),
    });
    state.personaGrowth[personaId] = {
      ...growth,
      feedback_error: "",
      latest_reviewed_change: { ...growth.latest_reviewed_change, feedback: data.feedback },
      reviewed_changes: (growth.reviewed_changes || []).map((change) => (
        Number(change.version) === Number(data.feedback.reviewed_version)
          ? {
              ...change,
              feedback: data.feedback.reaction === "needs_adjustment"
                ? {
                    reaction: data.feedback.reaction,
                    followup_status: data.feedback.followup_status || "waiting",
                  }
                : { reaction: data.feedback.reaction },
            }
          : change
      )),
    };
  } catch (err) {
    state.personaGrowth[personaId] = { ...growth, feedback_error: err.message };
  }
  renderShell();
}

function detailLine(label, value) {
  return h("div", { class: "detail-line" }, [h("span", { text: label }), h("p", { text: value })]);
}

function renderPersonaEditForm(persona) {
  const name = h("input", { name: "name", value: persona.name || "", maxlength: "40", placeholder: "给 TA 起个名字" });
  const summary = h("textarea", {
    name: "summary",
    rows: "3",
    maxlength: "1000",
    placeholder: "TA 给你的整体感觉，或者你希望别人怎么理解 TA",
  }, persona.summary || "");
  const relationship = h("input", {
    name: "relationship",
    value: persona.relationship || "",
    maxlength: "120",
    placeholder: "朋友、恋人、倾听者、搭档...",
  });
  const speakingStyle = h("textarea", {
    name: "speaking_style",
    rows: "3",
    maxlength: "300",
    placeholder: "短句、少说教、会吐槽、温柔一点...",
  }, persona.speaking_style || "");
  const avatarFile = h("input", { name: "avatar_file", type: "file", accept: "image/png,image/jpeg,image/webp,image/gif" });
  const avatarUrl = h("input", { name: "avatar_url", value: persona.avatar_url || "", placeholder: "可选：粘贴图片链接" });
  const appearance = h("textarea", {
    name: "appearance_description",
    rows: "4",
    maxlength: "2000",
    placeholder: "TA 目前给人的外貌或气质参考",
  }, persona.appearance_description || "");
  const desired = h("textarea", {
    name: "desired_image",
    rows: "4",
    maxlength: "2000",
    placeholder: "你希望 TA 的头像/形象大概是什么感觉",
  }, persona.desired_image || "");
  const status = h("small", { class: "save-status" });
  const deleteStatus = h("small", { class: "save-status danger-text" });
  const deleteConfirm = h("input", {
    name: "delete_confirm_name",
    maxlength: "40",
    placeholder: `输入“${persona.name || "未命名"}”确认删除`,
    autocomplete: "off",
  });
  const deleteButton = h("button", {
    type: "button",
    class: "danger-button",
    text: "删除人格",
    disabled: "disabled",
    onclick: async () => {
      deleteStatus.textContent = "";
      try {
        await api(`/api/personas/${persona.id}/delete`, {
          method: "POST",
          body: JSON.stringify({ confirm_name: deleteConfirm.value }),
        });
        state.personaPanelOpen = false;
        state.threadPanelOpen = false;
        state.editingPersona = false;
        state.activePersona = null;
        state.activeConversationId = null;
        state.messages = [];
        try {
          localStorage.removeItem(lastActiveKey());
        } catch {
          // Local storage may be unavailable in restricted browser modes.
        }
        await loadMainData();
        state.view = state.personas.length ? "home" : "forge";
        renderShell();
      } catch (err) {
        deleteStatus.textContent = err.message;
      }
    },
  });
  deleteConfirm.addEventListener("input", () => {
    const matched = deleteConfirm.value.trim() === String(persona.name || "").trim();
    if (matched) deleteButton.removeAttribute("disabled");
    else deleteButton.setAttribute("disabled", "disabled");
  });
  const generateAvatar = async () => {
    status.textContent = "";
    try {
      status.textContent = "正在生成头像...";
      const data = await api(`/api/personas/${persona.id}/avatar/generate`, {
        method: "POST",
        body: JSON.stringify({ desired_image: desired.value }),
      });
      avatarUrl.value = data.url || data.persona?.avatar_url || "";
      if (data.persona) applyPersonaUpdate(data.persona);
      status.textContent = "头像已生成";
    } catch (err) {
      status.textContent = err.message;
    }
  };

  return h("section", { class: "rail-section" }, [
    h("div", { class: "section-head" }, [
      h("p", { class: "eyebrow", text: "Persona" }),
      h("button", {
        type: "button",
        class: "ghost compact",
        text: "取消",
        onclick: () => {
          state.editingPersona = false;
          renderShell();
        },
      }),
    ]),
    h("form", {
      class: "persona-edit-form",
      onsubmit: async (event) => {
        event.preventDefault();
        status.textContent = "";
        try {
          let avatarValue = avatarUrl.value;
          if (avatarFile.files?.[0]) {
            status.textContent = "正在上传头像...";
            avatarValue = await uploadAvatarFile(avatarFile.files[0]);
          }
          const data = await api(`/api/personas/${persona.id}`, {
            method: "PATCH",
            body: JSON.stringify({
              name: name.value,
              summary: summary.value,
              relationship: relationship.value,
              speaking_style: speakingStyle.value,
              avatar_url: avatarValue,
              appearance_description: appearance.value,
              desired_image: desired.value,
            }),
          });
          applyPersonaUpdate(data.persona);
          delete state.personaGrowth[Number(data.persona.id)];
          state.editingPersona = false;
          state.personaPanelOpen = false;
          renderShell();
        } catch (err) {
          status.textContent = err.message;
        }
      },
    }, [
      h("label", {}, ["名字", name]),
      h("label", {}, ["关系定位", relationship]),
      h("label", {}, ["说话方式", speakingStyle]),
      h("label", {}, ["摘要", summary]),
      h("label", {}, ["头像图片", avatarFile]),
      h("label", {}, ["图片链接（可选）", h("div", { class: "avatar-url-row" }, [
        avatarUrl,
        h("button", { type: "button", class: "ghost compact", text: "生成头像", onclick: generateAvatar }),
      ])]),
      h("details", { class: "persona-edit-more" }, [
        h("summary", { text: "形象与更多资料" }),
        h("label", {}, ["外貌参考", appearance]),
        h("label", {}, ["期望形象", desired]),
      ]),
      h("div", { class: "actions rail-actions" }, [
        h("button", { type: "submit", text: "保存" }),
        h("button", {
          type: "button",
          class: "ghost",
          text: "取消",
          onclick: () => {
            state.editingPersona = false;
            renderShell();
          },
        }),
      ]),
      status,
      h("details", { class: "persona-danger-zone" }, [
        h("summary", { text: "删除这个人格" }),
        h("p", { text: "删除后，TA 和相关聊天会从你的主界面隐藏。记忆与版本历史暂时保留，以便后续支持恢复或数据处理。" }),
        h("label", {}, ["输入人格名字确认", deleteConfirm]),
        deleteButton,
        deleteStatus,
      ]),
    ]),
  ]);
}

function profileList(value) {
  if (!Array.isArray(value) || !value.length) return "";
  return value.join("；");
}

function selectControl(values, selectedValue, labels = values) {
  const select = h("select");
  values.forEach((value, index) => {
    const option = h("option", { value, text: labels[index] ?? value });
    if (String(value) === String(selectedValue || "")) option.selected = true;
    select.append(option);
  });
  return select;
}

function yearOptions() {
  const current = new Date().getFullYear();
  const years = [];
  for (let year = current; year >= 1920; year -= 1) years.push(String(year));
  return years;
}

function rangeOptions(start, end) {
  const values = [];
  for (let value = start; value <= end; value += 1) values.push(String(value).padStart(2, "0"));
  return values;
}

function parseBirthday(value) {
  const match = String(value || "").match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!match) return { year: "", month: "", day: "" };
  return { year: match[1], month: match[2], day: match[3] };
}

function buildBirthday(year, month, day) {
  if (!year || !month || !day) return "";
  return `${year}-${month}-${day}`;
}

function daysInMonth(year, month) {
  if (!year || !month) return 31;
  return new Date(Number(year), Number(month), 0).getDate();
}

function isAdmin() {
  return state.user?.role === "admin";
}

function renderWizard() {
  const selections = {};
  for (const key of Object.keys(state.options || {})) selections[key] = [];

  const panel = h("section", { class: "wizard" });
  const groups = h("div", { class: "wizard-groups" });
  const description = h("textarea", {
    rows: "6",
    maxlength: "2000",
    placeholder: "可以写：我想和一个冷静、不说教、但会记得我的人聊天。也可以写 TA 给你的感觉、希望的形象，或者你不喜欢怎样被关心。",
  });

  for (const [key, values] of forgeGroups()) {
    const group = h("div", { class: "option-group" }, [
      h("div", { class: "option-title" }, [
        h("h3", { text: groupTitle(key) }),
        h("small", { text: groupHint(key) }),
      ]),
    ]);
    for (const value of values) {
      const chip = h("button", {
        type: "button",
        class: "chip",
        text: value,
        onclick: () => {
          const selected = selections[key];
          const idx = selected.indexOf(value);
          if (idx >= 0) selected.splice(idx, 1);
          else if (selected.length < 2) selected.push(value);
          chip.classList.toggle("active", selected.includes(value));
        },
      });
      group.append(chip);
    }
    groups.append(group);
  }

  const starterList = h("div", { class: "starter-list" });
  for (const prompt of starterPrompts) {
    starterList.append(
      h("button", {
        type: "button",
        class: "starter-chip",
        text: prompt,
        onclick: () => appendDescriptionLine(description, prompt),
      })
    );
  }

  const error = h("p", { class: "error" });
  const fallbackName = h("input", {
    maxlength: "40",
    placeholder: "给 TA 写下一个名字",
    autocomplete: "off",
  });
  const namingFallback = h("section", { class: "naming-fallback", hidden: "hidden" }, [
    h("strong", { text: "这次没有取得合适的名字" }),
    h("p", { class: "muted", text: "可以再次生成，也可以由你先写下一个名字，之后仍然可以在资料里修改。" }),
    h("div", { class: "naming-fallback-actions" }, [
      fallbackName,
      h("button", {
        type: "button",
        class: "ghost",
        text: "用这个名字创建",
        onclick: () => {
          const preferredName = fallbackName.value.trim();
          if (!preferredName) {
            error.textContent = "先写下一个名字，或者点击直接生成再次尝试。";
            fallbackName.focus();
            return;
          }
          submitPersona(preferredName);
        },
      }),
    ]),
  ]);
  async function submitPersona(preferredName = "") {
    create.disabled = true;
    create.textContent = text.waiting;
    error.textContent = "";
    try {
      const data = await api("/api/personas", {
        method: "POST",
        body: JSON.stringify({ selections, description: description.value, preferred_name: preferredName || null }),
      });
      state.personas = [data.persona, ...state.personas.filter((item) => Number(item.id) !== Number(data.persona.id))];
      state.activePersona = data.persona;
      state.activeConversationId = null;
      state.messages = [];
      state.view = "chat";
      state.editingPersona = false;
      state.personaPanelOpen = false;
      state.focusComposer = true;
      renderShell();
    } catch (err) {
      error.textContent = err.message;
      if (err.message.includes("没有取得合适的名字")) {
        namingFallback.removeAttribute("hidden");
        fallbackName.focus();
      }
      create.disabled = false;
      create.textContent = text.directGenerate;
    }
  }
  const create = h("button", {
    type: "button",
    text: text.directGenerate,
    onclick: () => submitPersona(),
  });

  const actions = [create];
  if (state.activePersona) {
    actions.push(
      h("button", {
        type: "button",
        class: "ghost",
        text: "返回主界面",
        onclick: returnToHome,
      })
    );
  }

  const topbar = h("div", { class: "view-topbar" }, [
    state.activePersona
      ? h("button", {
          type: "button",
          class: "ghost compact",
          text: "返回",
          onclick: () => {
            state.view = "home";
            state.editingPersona = false;
            state.personaPanelOpen = false;
            renderShell();
          },
        })
      : h("span", { class: "topbar-placeholder" }),
    state.deletedPersonas.length
      ? h("button", {
          type: "button",
          class: "ghost compact",
          text: "已删除",
          onclick: () => {
            state.deletedPanelOpen = true;
            renderShell();
          },
        })
      : null,
  ]);

  panel.append(
    topbar,
    h("div", { class: "wizard-copy" }, [
      h("p", { class: "eyebrow", text: "初次相遇" }),
      h("h2", { text: text.wizardTitle }),
      h("p", {
        class: "muted",
        text: "点一两个感觉对的提示就够，也可以直接写下期待。第一次见面不必定义清楚，相处中 TA 会更贴近你的节奏。",
      }),
    ]),
    groups,
    h("section", { class: "free-text" }, [
      h("div", { class: "section-head" }, [
        h("strong", { text: text.optionalDescription }),
        h("small", { text: "可空" }),
      ]),
      starterList,
      description,
    ]),
    namingFallback,
    h("div", { class: "actions" }, actions),
    error
  );

  return panel;
}

function appendDescriptionLine(description, value) {
  description.value = description.value.trim() ? `${description.value.trim()}\n${value}` : value;
  description.focus();
}

function forgeGroups() {
  const options = state.options || {};
  return ["atmosphere", "relationship", "style", "boundaries"].map((key) => [key, (options[key] || []).slice(0, 4)]);
}

function renderComposer() {
  const input = h("textarea", { rows: "1", maxlength: "8000", placeholder: text.messagePlaceholder });
  const button = h("button", { type: "submit", text: state.sending ? "等待" : text.send, disabled: state.sending ? "disabled" : null });
  const form = h("form", { class: "composer" }, [input, button]);
  input.value = loadDraft();
  requestAnimationFrame(() => {
    autoResizeTextarea(input);
    if (state.focusComposer) {
      input.focus();
      input.setSelectionRange(input.value.length, input.value.length);
      state.focusComposer = false;
    }
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const content = input.value.trim();
    if (!content || state.sending) return;
    if (state.view === "group" && !state.activeGroupConversationId) return;
    if (state.view !== "group" && !state.activePersona) return;
    input.value = "";
    clearDraft();
    autoResizeTextarea(input);
    if (state.view === "group") await sendGroupChatMessage(content);
    else await sendChatMessage(content);
  });
  input.addEventListener("input", () => {
    saveDraft(input.value);
    autoResizeTextarea(input);
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      form.requestSubmit();
    }
  });
  return form;
}

function draftKey() {
  if (state.view === "group") {
    const groupId = state.activeGroupConversationId || "new";
    return `mnemosyne:group-draft:${groupId}`;
  }
  const personaId = state.activePersona?.id || "none";
  const conversationId = state.activeConversationId || "new";
  return `mnemosyne:draft:${personaId}:${conversationId}`;
}

function groupAutoTurnKey(groupId) {
  return `mnemosyne:group-auto-turn:${groupId}`;
}

function groupAutoTurnEnabled(groupId) {
  try {
    return localStorage.getItem(groupAutoTurnKey(groupId)) !== "0";
  } catch {
    return true;
  }
}

function setGroupAutoTurnEnabled(groupId, enabled) {
  try {
    localStorage.setItem(groupAutoTurnKey(groupId), enabled ? "1" : "0");
  } catch {
    // Local storage may be unavailable in restricted browser modes.
  }
}

function loadDraft() {
  try {
    return localStorage.getItem(draftKey()) || "";
  } catch {
    return "";
  }
}

function saveDraft(value) {
  try {
    const key = draftKey();
    if (value) localStorage.setItem(key, value);
    else localStorage.removeItem(key);
  } catch {
    // Local storage may be unavailable in restricted browser modes.
  }
}

function setComposerDraft(value) {
  saveDraft(value);
  state.focusComposer = true;
  renderShell();
}

function clearDraft() {
  try {
    localStorage.removeItem(draftKey());
  } catch {
    // Ignore unavailable local storage.
  }
}

function autoResizeTextarea(input) {
  input.style.height = "auto";
  const nextHeight = Math.min(input.scrollHeight, 160);
  input.style.height = `${Math.max(44, nextHeight)}px`;
}

async function sendChatMessage(content, { retryLocalId = "", retryUserMessageId = null, clientMessageId = "" } = {}) {
  if (!content || !state.activePersona || state.sending) return;
  const requestPersonaId = Number(state.activePersona.id);
  const outgoingClientMessageId = clientMessageId || (retryUserMessageId ? "" : createClientMessageId());
  state.sending = true;
  if (retryLocalId) {
    state.messages = state.messages.filter((message) => message.local_id !== retryLocalId);
  }
  if (!retryUserMessageId && !clientMessageId) {
    state.messages.push({ role: "user", content, client_message_id: outgoingClientMessageId, created_at: nowSeconds() });
  }
  renderShell();
  scrollChat();
  try {
    const data = await api("/api/chat", {
      method: "POST",
      body: JSON.stringify({
        message: content,
        persona_id: state.activePersona.id,
        conversation_id: state.activeConversationId,
        retry_user_message_id: retryUserMessageId,
        client_message_id: outgoingClientMessageId || null,
      }),
    });
    const stillViewingConversation = (
      state.view === "chat"
      && Number(state.activePersona?.id) === requestPersonaId
    );
    if (stillViewingConversation) {
      state.activeConversationId = data.conversation_id;
      if (data.degraded) {
        state.messages.push({
          role: "notice",
          content: data.error_message || "回复暂时没有送达。可以稍后重试。",
          status: data.pending ? "pending" : "error",
          retry_content: content,
          retry_user_message_id: data.pending ? null : data.user_message_id,
          retry_client_message_id: outgoingClientMessageId,
          local_id: `error-${Date.now()}`,
          created_at: nowSeconds(),
        });
      } else {
        state.messages.push({
          role: "assistant",
          content: data.reply,
          expressions: data.expressions || [],
          status: "sent",
          id: data.assistant_message_id,
          created_at: nowSeconds(),
        });
        await markConversationRead(data.conversation_id);
      }
    }
    await loadMainData();
  } catch (err) {
    if (state.view === "chat" && Number(state.activePersona?.id) === requestPersonaId) {
      state.messages.push({
        role: "notice",
        content: friendlyChatError(err.message),
        status: "error",
        retry_content: content,
        retry_client_message_id: outgoingClientMessageId,
        local_id: `error-${Date.now()}`,
        created_at: nowSeconds(),
      });
    }
  }
  state.sending = false;
  renderShell();
  scrollChat();
}

async function sendGroupChatMessage(content, { retryLocalId = "", clientMessageId = "" } = {}) {
  if (!content || !state.activeGroupConversationId || state.sending) return;
  const requestGroupId = Number(state.activeGroupConversationId);
  const outgoingClientMessageId = clientMessageId || createClientMessageId();
  state.sending = true;
  if (retryLocalId) {
    state.groupMessages = state.groupMessages.filter((message) => message.local_id !== retryLocalId);
  }
  if (!clientMessageId) {
    state.groupMessages.push({
      speaker_type: "user",
      content,
      client_message_id: outgoingClientMessageId,
      created_at: nowSeconds(),
    });
  }
  renderShell();
  scrollChat();
  try {
    const data = await api("/api/group-chat", {
      method: "POST",
      body: JSON.stringify({
        message: content,
        group_conversation_id: requestGroupId,
        client_message_id: outgoingClientMessageId,
      }),
    });
    const stillViewingGroup = state.view === "group" && Number(state.activeGroupConversationId) === requestGroupId;
    if (stillViewingGroup) {
      state.groupMessages = state.groupMessages.filter((message) => message.client_message_id !== outgoingClientMessageId);
      state.groupMessages.push(...(data.messages || []));
      const hasStoredRetryNotice = (data.messages || []).some((message) => (
        message.speaker_type === "user"
        && message.client_message_id === outgoingClientMessageId
        && ["error", "generating"].includes(message.reply_status)
      ));
      if (data.degraded && !(data.replies || []).length && !hasStoredRetryNotice) {
        state.groupMessages.push({
          speaker_type: "system",
          role: "notice",
          content: data.error_message || "这轮群聊暂时没有成功接上。稍后再试一次。",
          status: "error",
          retry_content: content,
          retry_client_message_id: outgoingClientMessageId,
          local_id: `group-degraded-${Date.now()}`,
          created_at: nowSeconds(),
        });
      }
      await markGroupConversationRead(requestGroupId);
    }
    await loadMainData();
  } catch (err) {
    if (state.view === "group" && Number(state.activeGroupConversationId) === requestGroupId) {
      state.groupMessages.push({
        speaker_type: "system",
        role: "notice",
        content: friendlyChatError(err.message),
        status: "error",
        retry_content: content,
        retry_client_message_id: outgoingClientMessageId,
        local_id: `group-error-${Date.now()}`,
        created_at: nowSeconds(),
      });
    }
  }
  state.sending = false;
  renderShell();
  scrollChat();
}

async function markConversationRead(conversationId) {
  if (!conversationId) return;
  try {
    await api(`/api/conversations/${conversationId}/read`, { method: "POST" });
  } catch {
    // Read markers are a convenience state; failing to update them should not block chat.
  }
}

async function markGroupConversationRead(groupConversationId) {
  if (!groupConversationId) return;
  try {
    await api(`/api/group-conversations/${groupConversationId}/read`, { method: "POST" });
  } catch {
    // Read markers are a convenience state; failing to update them should not block chat.
  }
}

async function maybeRequestGroupAutonomousTurn() {
  if (
    state.view !== "group"
    || !state.activeGroupConversationId
    || state.sending
    || state.groupSettingsOpen
    || document.hidden
  ) {
    return;
  }
  const messages = state.groupMessages || [];
  if (!messages.length) return;
  const now = nowSeconds();
  const latest = messages[messages.length - 1];
  if (now - Number(latest.created_at || 0) < GROUP_AUTO_MIN_IDLE_SECONDS) return;
  const latestUser = [...messages].reverse().find((message) => message.speaker_type === "user");
  if (!latestUser || now - Number(latestUser.created_at || 0) > GROUP_AUTO_USER_WINDOW_SECONDS) return;

  const groupId = Number(state.activeGroupConversationId);
  if (!groupAutoTurnEnabled(groupId)) return;
  const cooldownKey = String(groupId);
  const nextAllowedAt = Number(groupAutoCooldowns.get(cooldownKey) || 0);
  if (now < nextAllowedAt) return;
  groupAutoCooldowns.set(cooldownKey, now + GROUP_AUTO_MIN_IDLE_SECONDS);

  try {
    const data = await api(`/api/group-conversations/${groupId}/autonomous-turn`, {
      method: "POST",
      body: JSON.stringify({ client_message_id: createClientMessageId() }),
    });
    if (data.degraded) {
      groupAutoCooldowns.set(cooldownKey, nowSeconds() + GROUP_AUTO_FAILURE_BACKOFF_SECONDS);
    }
    if (state.view !== "group" || Number(state.activeGroupConversationId) !== groupId) return;
    if ((data.messages || []).length) {
      groupAutoCooldowns.set(cooldownKey, nowSeconds() + GROUP_AUTO_MIN_IDLE_SECONDS);
      state.groupMessages.push(...data.messages);
      await markGroupConversationRead(groupId);
      await loadMainData();
      renderShell();
      scrollChat();
    }
  } catch {
    groupAutoCooldowns.set(cooldownKey, nowSeconds() + GROUP_AUTO_FAILURE_BACKOFF_SECONDS);
    // Autonomous turns are opportunistic; visible errors belong to user-sent messages.
  }
}

function renderChatEmpty() {
  const persona = state.activePersona || {};
  return h("div", { class: "chat-empty" }, [
    avatar(persona.name || "忆", persona.avatar_url),
    h("h3", { text: persona.name || "新的对话对象" }),
    renderPersonaStatusLine(persona, "center"),
    h("p", { text: persona.summary || "从一句自然的话开始。你们的相处方式会在交谈里慢慢清晰起来。" }),
    h("div", { class: "chat-empty-actions" }, [
      chatStarterButton("今天有点累。"),
      chatStarterButton("想随便聊聊。"),
      chatStarterButton("陪我想一件事。"),
    ]),
  ]);
}

function chatStarterButton(value) {
  return h("button", {
    type: "button",
    class: "chat-empty-chip",
    text: value,
    onclick: () => setComposerDraft(value),
  });
}

function conversationPreview(item) {
  const content = String(item.last_message || "").trim();
  if (!content) return item.persona_name || "暂无消息";
  if (item.last_message_role === "user" && item.last_message_reply_status === "failed") {
    return `回复未送达 · 你：${content}`.slice(0, 42);
  }
  if (item.last_message_role === "user" && item.last_message_reply_status === "generating") {
    return `等待回复 · 你：${content}`.slice(0, 42);
  }
  const prefix = item.last_message_role === "user" ? "你：" : "";
  return `${prefix}${content}`.slice(0, 42);
}

function conversationDisplayTitle(item) {
  const title = String(item.title || "").trim();
  return title || item.persona_name || "聊天";
}

function groupConversationTitle(group) {
  const title = String(group?.title || "").trim();
  if (title) return title;
  const names = (group?.members || []).map((member) => member.display_name || member.name).filter(Boolean);
  return names.slice(0, 3).join("、") || "群聊";
}

function groupConversationPreview(group) {
  const content = String(group?.last_message || "").trim();
  if (content) return content.slice(0, 42);
  const names = (group?.members || []).map((member) => member.display_name || member.name).filter(Boolean);
  return names.length ? `${names.slice(0, 4).join("、")} 在群里` : "还没有消息";
}

function renderPersonaStatusLine(persona, align = "") {
  const storedRelation = cleanMetaText(persona?.relationship);
  const relation = !storedRelation || storedRelation === "关系未定" ? "刚刚认识" : storedRelation;
  const style = cleanMetaText(persona?.speaking_style) || "正在适应你的聊天节奏";
  const version = persona?.version ? `v${persona.version}` : "v1";
  return h("div", { class: `persona-status-line ${align}`.trim() }, [
    h("span", { class: "status-pill relation", text: relation }),
    persona?.growth_notice ? h("button", {
      type: "button",
      class: "status-pill growth action",
      text: "相处有变化 · 查看",
      onclick: (event) => openPersonaGrowthNotice(event, persona),
    }) : null,
    persona?.growth_action?.kind === "preference_retry" ? h("button", {
      type: "button",
      class: "status-pill preference-action action",
      text: "偏好需重新确认 · 查看",
      onclick: (event) => openPersonaGrowthNotice(event, persona),
    }) : null,
    h("span", { class: "status-pill", text: style }),
    h("span", { class: "status-pill quiet", text: version }),
  ]);
}

function renderInlinePersonaMeta(persona) {
  const relation = cleanMetaText(persona?.relationship);
  const style = cleanMetaText(persona?.speaking_style);
  const value = [relation, style].filter(Boolean).slice(0, 2).join(" · ");
  return value ? h("small", { class: "persona-inline-meta", text: value }) : null;
}

function cleanMetaText(value) {
  const text = String(value || "").trim();
  if (!text || text === "尚未明确" || text === "未设置") return "";
  return text;
}

function isPinnedConversation(item) {
  return Number(item?.pinned_at || 0) > 0;
}

function normalizedSearch(value) {
  return String(value || "").trim().toLowerCase();
}

function matchesConversationSearch(item, query) {
  if (!query) return true;
  return [
    conversationDisplayTitle(item),
    item.persona_name,
    item.title,
    item.last_message,
  ].some((value) => String(value || "").toLowerCase().includes(query));
}

function matchesPersonaSearch(persona, query) {
  if (!query) return true;
  return [
    persona.name,
    persona.summary,
    persona.relationship,
    persona.speaking_style,
  ].some((value) => String(value || "").toLowerCase().includes(query));
}

function matchesGroupSearch(group, query) {
  if (!query) return true;
  return [
    group.title,
    group.last_message,
    ...(group.members || []).map((member) => member.display_name || member.name),
  ].some((value) => String(value || "").toLowerCase().includes(query));
}

function renderMessageList(messages) {
  const nodes = [];
  let lastDateKey = "";
  for (const message of messages) {
    const dateKey = messageDateKey(message.created_at);
    if (dateKey && dateKey !== lastDateKey) {
      nodes.push(renderDateDivider(message.created_at));
      lastDateKey = dateKey;
    }
    nodes.push(renderMessage(message));
    if (message.role === "user" && message.reply_status === "failed") {
      nodes.push(renderFailedReplyNotice(message));
    } else if (isStalledReply(message)) {
      nodes.push(renderStalledReplyNotice(message));
    }
  }
  return nodes;
}

function renderGroupMessageList(messages) {
  const nodes = [];
  let lastDateKey = "";
  for (const message of messages) {
    const dateKey = messageDateKey(message.created_at);
    if (dateKey && dateKey !== lastDateKey) {
      nodes.push(renderDateDivider(message.created_at));
      lastDateKey = dateKey;
    }
    nodes.push(renderGroupMessage(message));
    if (isFailedGroupUserMessage(message)) {
      nodes.push(renderFailedGroupReplyNotice(message));
    }
  }
  return nodes;
}

function renderGroupMessage(message) {
  const isUser = message.speaker_type === "user";
  const isNotice = message.speaker_type === "system" || message.role === "notice";
  const speakerName = isUser ? (state.profile?.nickname || "我") : message.speaker_name || groupMemberName(message.speaker_persona_id) || "TA";
  const speakerAvatar = isUser
    ? state.profile?.avatar_url
    : message.speaker_avatar_url || groupMemberAvatar(message.speaker_persona_id);
  const content = String(message.content || "").trim();
  const bubbleChildren = [content];
  if (isNotice && ["error", "pending"].includes(message.status) && message.retry_content) {
    bubbleChildren.push(
      h("button", {
        type: "button",
        class: "retry-btn",
        text: "重试",
        onclick: () => sendGroupChatMessage(message.retry_content, {
          retryLocalId: message.local_id,
          clientMessageId: message.retry_client_message_id || "",
        }),
      })
    );
  }
  return h("article", { class: `message group-message ${isUser ? "user" : isNotice ? "notice" : "assistant"}` }, [
    !isUser && !isNotice ? avatar(speakerName, speakerAvatar) : null,
    h("div", { class: "message-stack" }, [
      !isUser && !isNotice ? h("small", { class: "group-speaker-name", text: speakerName }) : null,
      h("div", { class: `bubble ${message.status || ""}` }, bubbleChildren),
      !isUser && !isNotice && Array.isArray(message.expressions) && message.expressions.length
        ? renderExpressionStrip(message.expressions)
        : null,
      h("div", { class: "message-meta" }, [
        h("small", { class: "message-time", text: formatMessageTime(message.created_at) }),
        h("button", {
          type: "button",
          class: "copy-message",
          text: "澶嶅埗",
          onclick: () => copyMessageText(message),
        }),
      ]),
    ]),
    isUser ? avatar(speakerName, speakerAvatar) : null,
  ]);
}

function isFailedGroupUserMessage(message) {
  return (
    message.speaker_type === "user"
    && ["error", "generating"].includes(message.reply_status)
    && Boolean(message.client_message_id)
  );
}

function renderFailedGroupReplyNotice(userMessage) {
  return renderGroupMessage({
    speaker_type: "system",
    role: "notice",
    content: userMessage.reply_error || "这句话还没有等到群聊回复，可以重试一次。",
    status: userMessage.reply_status === "generating" ? "pending" : "error",
    retry_content: userMessage.content,
    retry_client_message_id: userMessage.client_message_id,
    local_id: `group-stored-error-${userMessage.id}`,
    created_at: userMessage.created_at,
  });
}

function renderFailedReplyNotice(userMessage) {
  return renderMessage({
    role: "notice",
    content: userMessage.reply_error || "回复暂时没有送达。可以稍后重试。",
    status: "error",
    retry_content: userMessage.content,
    retry_user_message_id: userMessage.id,
    retry_client_message_id: userMessage.client_message_id,
    local_id: `stored-error-${userMessage.id}`,
    created_at: userMessage.created_at,
  });
}

function isStalledReply(message) {
  return (
    message.role === "user"
    && message.reply_status === "generating"
    && Boolean(message.client_message_id)
    && nowSeconds() - Number(message.created_at || 0) >= 120
  );
}

function renderStalledReplyNotice(userMessage) {
  return renderMessage({
    role: "notice",
    content: "这句话还没有等到回复。可以重新尝试一次。",
    status: "pending",
    retry_content: userMessage.content,
    retry_client_message_id: userMessage.client_message_id,
    local_id: `stalled-${userMessage.id}`,
    created_at: userMessage.created_at,
  });
}

function renderDateDivider(value) {
  return h("div", { class: "date-divider" }, [
    h("span", { text: formatMessageDate(value) }),
  ]);
}

function renderMessage(message) {
  const isUser = message.role === "user";
  const isNotice = message.role === "notice";
  const segments = messageSegments(message);
  return h("article", { class: `message ${isUser ? "user" : isNotice ? "notice" : "assistant"}` }, [
    !isUser && !isNotice ? avatar(state.activePersona?.name || "忆", state.activePersona?.avatar_url) : null,
    h("div", { class: "message-stack" }, [
      !isUser && !isNotice && Array.isArray(message.expressions) && message.expressions.length
        ? renderExpressionStrip(message.expressions)
        : null,
      ...segments.map((segment, index) => {
        const children = [segment];
        if (index === segments.length - 1 && ["error", "pending"].includes(message.status) && message.retry_content) {
          children.push(
            h("button", {
              type: "button",
              class: "retry-btn",
              text: "重试",
              onclick: () => sendChatMessage(message.retry_content, {
                retryLocalId: message.local_id,
                retryUserMessageId: message.retry_user_message_id || null,
                clientMessageId: message.retry_client_message_id || "",
              }),
            })
          );
        }
        return h("div", { class: `bubble ${message.status || ""}` }, children);
      }),
      h("div", { class: "message-meta" }, [
        h("small", { class: "message-time", text: formatMessageTime(message.created_at) }),
        h("button", {
          type: "button",
          class: "copy-message",
          text: "复制",
          onclick: () => copyMessageText(message),
        }),
      ]),
    ]),
    isUser ? avatar(state.profile?.nickname || "U", state.profile?.avatar_url) : null,
  ]);
}

async function copyMessageText(message) {
  const content = String(message.content || "").trim();
  if (!content) return;
  try {
    await navigator.clipboard.writeText(content);
  } catch {
    const area = h("textarea", { class: "clipboard-fallback" }, content);
    document.body.append(area);
    area.select();
    document.execCommand("copy");
    area.remove();
  }
}

function messageSegments(message) {
  const content = String(message.content || "").trim();
  if (!content) return [""];
  if (message.role !== "assistant" || message.status === "error") return [content];
  return splitAssistantContent(content);
}

function splitAssistantContent(content) {
  const text = String(content || "").replace(/\n{3,}/g, "\n\n").trim();
  if (text.length <= 72 && !text.includes("\n")) return [text];
  const rawParts = text
    .split(/(?<=[。！？!?~～])\s+|(?<=[。！？!?~～])|[\r\n]+/)
    .map((part) => part.trim())
    .filter(Boolean);
  const parts = rawParts.length ? rawParts : [text];
  const segments = [];
  let current = "";
  for (const part of parts) {
    if (!current) {
      current = part;
      continue;
    }
    if ((current + part).length <= 56) {
      current += part;
    } else {
      segments.push(current);
      current = part;
    }
  }
  if (current) segments.push(current);
  return segments.flatMap((segment) => splitLongSegment(segment, 92)).slice(0, 8);
}

function splitLongSegment(text, maxLength) {
  if (text.length <= maxLength) return [text];
  const pieces = [];
  let rest = text;
  while (rest.length > maxLength) {
    let cut = Math.max(rest.lastIndexOf("，", maxLength), rest.lastIndexOf("、", maxLength), rest.lastIndexOf("；", maxLength));
    if (cut < 28) cut = maxLength;
    pieces.push(rest.slice(0, cut + 1).trim());
    rest = rest.slice(cut + 1).trim();
  }
  if (rest) pieces.push(rest);
  return pieces;
}

function renderExpressionStrip(expressions) {
  return h("div", { class: "expression-strip" }, expressions.slice(0, 3).map((item) => (
    renderExpressionPill(item)
  )));
}

function renderExpressionPill(item) {
  const asset = expressionAssetFor(item);
  const type = asset?.expression_type || item.expression_type || item.type || "gesture";
  const label = asset?.display_text || item.label || item.source_text || "";
  const icon = asset?.icon || "";
  const assetKind = asset?.asset_kind || "text_badge";
  const intensity = asset?.intensity ? `intensity-${asset.intensity}` : "intensity-1";
  return h("span", {
    class: `expression-pill ${type} ${assetKind} ${intensity}`,
    title: asset?.description || label,
  }, [
    icon ? h("span", { class: "expression-icon", text: icon }) : null,
    h("span", { text: label }),
  ]);
}

function expressionAssetFor(item) {
  const type = String(item.expression_type || item.type || "").trim();
  const label = String(item.label || "").trim();
  return (state.expressionAssets || []).find((asset) => (
    String(asset.expression_type || "") === type && String(asset.label || "") === label
  ));
}

function replaceLocalMessage(localId, replacement) {
  state.messages = state.messages.map((message) => message.local_id === localId ? replacement : message);
}

function friendlyChatError(message) {
  const text = String(message || "");
  if (text.includes("429") || text.includes("Too Many Requests") || text.includes("服务暂时")) {
    return "服务现在有点拥堵，刚才这句话已经留在当前会话里。稍后再试一次就好。";
  }
  return "刚才没有成功发出去。稍后再试一次。";
}

function nowSeconds() {
  return Math.floor(Date.now() / 1000);
}

function createClientMessageId() {
  if (window.crypto?.randomUUID) return window.crypto.randomUUID();
  return `${Date.now()}-${Math.random().toString(36).slice(2, 14)}`;
}

function formatMessageTime(value) {
  if (!value) return "";
  const date = new Date(Number(value) * 1000);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false });
}

function messageDateKey(value) {
  if (!value) return "";
  const date = new Date(Number(value) * 1000);
  if (Number.isNaN(date.getTime())) return "";
  return `${date.getFullYear()}-${date.getMonth() + 1}-${date.getDate()}`;
}

function formatMessageDate(value) {
  if (!value) return "";
  const date = new Date(Number(value) * 1000);
  if (Number.isNaN(date.getTime())) return "";
  const today = new Date();
  const yesterday = new Date();
  yesterday.setDate(today.getDate() - 1);
  if (date.toDateString() === today.toDateString()) return "今天";
  if (date.toDateString() === yesterday.toDateString()) return "昨天";
  if (date.getFullYear() === today.getFullYear()) {
    return date.toLocaleDateString("zh-CN", { month: "long", day: "numeric" });
  }
  return date.toLocaleDateString("zh-CN", { year: "numeric", month: "long", day: "numeric" });
}

function formatListTime(value) {
  if (!value) return "";
  const date = new Date(Number(value) * 1000);
  if (Number.isNaN(date.getTime())) return "";
  const now = new Date();
  const sameDay = date.toDateString() === now.toDateString();
  if (sameDay) return date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false });
  const sameYear = date.getFullYear() === now.getFullYear();
  if (sameYear) return date.toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" });
  return date.toLocaleDateString("zh-CN", { year: "2-digit", month: "2-digit", day: "2-digit" });
}

function avatar(name, url) {
  const initial = avatarText(name);
  const el = h("div", { class: "avatar", text: url ? "" : initial });
  if (url) {
    el.append(h("img", { src: url, alt: String(name || "avatar") }));
  }
  return el;
}

function groupAvatar(group) {
  const members = group?.members || [];
  const first = members[0] || {};
  const second = members[1] || {};
  return h("div", { class: "group-avatar" }, [
    avatar(first.display_name || first.name || "群", first.avatar_url),
    avatar(second.display_name || second.name || "+", second.avatar_url),
  ]);
}

function groupMemberName(personaId) {
  const member = (state.activeGroupConversation?.members || []).find((item) => Number(item.persona_id) === Number(personaId));
  return member?.display_name || member?.name || "";
}

function groupMemberAvatar(personaId) {
  const member = (state.activeGroupConversation?.members || []).find((item) => Number(item.persona_id) === Number(personaId));
  return member?.avatar_url || "";
}

function avatarText(name) {
  const text = String(name || "").trim();
  if (!text) return "忆";
  if (/^[A-Za-z0-9_\-\s]+$/.test(text)) return text.replace(/\s+/g, "").slice(0, 2).toUpperCase();
  return text.slice(0, 1);
}

function groupTitle(key) {
  return {
    atmosphere: "氛围",
    relationship: "关系",
    style: "说话方式",
    boundaries: "边界",
  }[key] || key;
}

function groupHint(key) {
  return {
    atmosphere: "TA 在场时的感觉",
    relationship: "你们大概怎么相处",
    style: "回复的节奏和语气",
    boundaries: "先划清不舒服的地方",
  }[key] || "";
}

async function logout() {
  const isolated = tabSessionMode();
  await api("/api/auth/logout", { method: "POST" }).catch(() => {});
  prepareAuthMode(isolated);
  resetClientState();
  renderAuth("login");
}

async function switchAccountInThisTab() {
  if (tabSessionMode() && tabSessionToken()) {
    await api("/api/auth/logout", { method: "POST" }).catch(() => {});
  }
  prepareAuthMode(true);
  resetClientState();
  renderAuth("login");
}

function resetClientState() {
  state = {
    user: null,
    profile: null,
    options: null,
    expressionAssets: [],
    personas: [],
    deletedPersonas: [],
    personaGrowth: {},
    loadingGrowthPersonaId: null,
    conversations: [],
    archivedConversations: [],
    groupConversations: [],
    archivedGroupConversations: [],
    activePersona: null,
    activeConversationId: null,
    activeGroupConversationId: null,
    activeGroupConversation: null,
    messages: [],
    groupMessages: [],
    view: "chat",
    editingPersona: false,
    profileOpen: false,
    personaPanelOpen: false,
    threadPanelOpen: false,
    deletedPanelOpen: false,
    groupCreateOpen: false,
    groupSettingsOpen: false,
    editingConversationId: null,
    showArchived: false,
    showArchivedGroups: false,
    conversationSearch: "",
    focusComposer: false,
    sending: false,
  };
}

function scrollChat() {
  requestAnimationFrame(() => {
    const log = document.getElementById("chat-log");
    if (log) log.scrollTop = log.scrollHeight;
  });
}

document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  if (state.profileOpen || state.personaPanelOpen || state.threadPanelOpen || state.deletedPanelOpen || state.groupCreateOpen) {
    state.profileOpen = false;
    state.personaPanelOpen = false;
    state.threadPanelOpen = false;
    state.deletedPanelOpen = false;
    state.groupCreateOpen = false;
    state.editingPersona = false;
    renderShell();
    return;
  }
  if (state.view === "forge" && state.activePersona) {
    state.view = "chat";
    state.editingPersona = false;
    renderShell();
  }
});

setInterval(() => {
  maybeRequestGroupAutonomousTurn();
}, GROUP_AUTO_CHECK_MS);

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    maybeRequestGroupAutonomousTurn();
  }
});

bootstrap();
