export function mount(context) {
  const { document, read, action, signal, isCurrent, onCatalogChanged } = context;
  const root = document.createElement("div");
  root.hidden = true;
  const style = new document.defaultView.CSSStyleSheet();
  style.replaceSync(`
    .agy-account-card { display: grid; gap: 12px; padding: 16px; border: 1px solid var(--hairline); border-radius: var(--radius-sm); }
    .agy-account-card h3 { margin: 0; font-size: 14px; }
    .agy-card-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
    .agy-test-controls, .agy-code-controls { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; }
    .agy-test-controls > select, .agy-test-controls > .custom-select, .agy-code-controls > input { flex: 1 1 200px; min-width: 0; }
    .agy-operation-message, .agy-test-reply { margin: 0; max-height: 180px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; }
    .agy-test-reply { padding: 12px; background: var(--tint-faint); border-radius: var(--radius-sm); font-size: 13px; }
    .agy-copy-details { justify-self: start; }
    .agy-feedback { display: grid; gap: 8px; min-width: 0; }
    .agy-toggle-row { display: flex; align-items: center; gap: 16px; flex-wrap: wrap; font-size: 13px; }
    .agy-toggle-row label { display: inline-flex; align-items: center; gap: 6px; cursor: pointer; }
  `);
  document.adoptedStyleSheets = [...document.adoptedStyleSheets, style];
  const panels = new Map();
  let snapshot = "";
  let disposed = false;
  const current = () => !disposed && !signal.aborted && isCurrent();
  const serviceId = profileId => `sakura.model.antigravity:${profileId}`;
  const catalogValue = profiles =>
    JSON.stringify(
      profiles.map(({ profileId, label, createdAt, loggedIn, models }) => ({
        profileId,
        label,
        createdAt,
        loggedIn,
        models,
      })),
    );

  function apply(result) {
    if (!current() || !result.values?.profiles) return;
    const profiles = result.values.profiles;
    const catalogChanged =
      catalogValue(profiles) !==
      catalogValue([...panels.values()].map(panel => panel.value()));
    for (const [id, panel] of panels) {
      if (!profiles.some(profile => profile.profileId === id)) {
        panel.dispose();
        panels.delete(id);
      }
    }
    for (const profile of profiles) {
      if (panels.has(profile.profileId)) panels.get(profile.profileId).update(profile);
      else panels.set(profile.profileId, createAccountPanel(context, profile, apply));
    }
    if (catalogChanged) onCatalogChanged();
  }

  function update() {
    const profiles = read("profiles") || [];
    const next = JSON.stringify(profiles);
    if (snapshot !== next) {
      snapshot = next;
      apply({ values: { profiles } });
    }
  }
  update();
  return {
    element: root,
    update,
    cancel: () => Promise.all([...panels.values()].map(panel => panel.cancel())),
    contributions: () => ({
      modelServicePresets: [
        {
          id: "sakura.model.antigravity",
          label: "Google Antigravity",
          iconName: "sparkles",
          async add({ createdAt }) {
            const result = await action("add", { operationRequest: { createdAt } });
            if (!current()) return;
            apply(result);
            return serviceId(result.values.profiles.at(-1).profileId);
          },
        },
      ],
      modelServices: [...panels.values()].map(panel => {
        const profile = panel.value();
        const cliText = profile.cliVersion ? `CLI ${profile.cliVersion}` : "需配置或检测 agy CLI";
        return {
          id: serviceId(profile.profileId),
          label: profile.label,
          iconName: "sparkles",
          createdAt: profile.createdAt,
          description: profile.loggedIn
            ? `${cliText} · ${profile.models.length} 个模型`
            : cliText,
          configured: Boolean(profile.loggedIn && profile.models.length),
          models: (profile.models || []).map(model => model.modelId),
          element: panel.element,
          cancel: panel.cancel,
          select: panel.select,
        };
      }),
      modelCatalog: [
        {
          serviceKey: "sakura.model.antigravity",
          label: "Google Antigravity",
          profiles: [...panels.values()].map(panel => {
            const { profileId, label, models } = panel.value();
            return { profileId, label, models };
          }),
        },
      ],
    }),
    dispose() {
      disposed = true;
      for (const panel of panels.values()) panel.dispose();
      document.adoptedStyleSheets = document.adoptedStyleSheets.filter(
        sheet => sheet !== style,
      );
      root.remove();
    },
  };
}

function createAccountPanel(context, initial, apply) {
  const {
    document,
    window,
    action,
    signal,
    isCurrent,
    onError,
    createIcon,
    enhanceSelect,
    refreshSelect,
    closeSelects,
  } = context;
  const node = (tag, className = "", text = "") => {
    const element = document.createElement(tag);
    element.className = className;
    element.textContent = text;
    return element;
  };
  const panel = node("div", "provider-account");
  const heading = node("h2", "provider-account-title");
  const title = node("span");
  heading.append(createIcon(document, "sparkles"), title);

  const nameRow = node("div", "form-row");
  const label = node("label", "", "名称");
  const name = node("input", "wide-input");
  name.type = "text";
  name.id = `agy-name-${initial.profileId}`;
  label.htmlFor = name.id;
  name.value = initial.label;
  nameRow.append(label, name);

  const accountLabel = node("p", "provider-account-identity");
  const hint = node("p", "detail-desc");
  const status = node("p", "detail-desc agy-operation-message");
  status.setAttribute("role", "status");
  const reply = node("pre", "agy-test-reply");
  const feedback = node("div", "agy-feedback");
  const generalFeedback = node("div");

  // CLI and connection config card
  const configCard = node("section", "agy-account-card");
  configCard.append(node("h3", "", "ACP 连接参数"));

  const cmdRow = node("div", "form-row");
  const cmdLabel = node("label", "", "CLI 路径（可选）");
  const cmdInput = node("input", "wide-input");
  cmdInput.type = "text";
  cmdInput.placeholder = "留空自动查找系统 agy，或填写绝对路径";
  cmdInput.value = initial.command || "";
  cmdRow.append(cmdLabel, cmdInput);

  const thoughtRow = node("div", "form-row");
  const thoughtLabel = node("label", "", "思考档位");
  const thoughtSelect = node("select");
  for (const [val, text] of [
    ["default", "默认 (default)"],
    ["low", "低 (low)"],
    ["medium", "中 (medium)"],
    ["high", "高 (high)"],
  ]) {
    const opt = node("option", "", text);
    opt.value = val;
    thoughtSelect.append(opt);
  }
  thoughtSelect.value = initial.thought_level || "default";
  thoughtRow.append(thoughtLabel, thoughtSelect);

  const proxyRow = node("div", "form-row");
  const proxyLabel = node("label", "", "代理模式");
  const proxySelect = node("select");
  for (const [val, text] of [
    ["inherit", "跟随系统 (inherit)"],
    ["direct", "直连 (direct)"],
    ["custom", "自定义代理 (custom)"],
  ]) {
    const opt = node("option", "", text);
    opt.value = val;
    proxySelect.append(opt);
  }
  proxySelect.value = initial.proxy_mode || "inherit";
  proxyRow.append(proxyLabel, proxySelect);

  const proxyUrlRow = node("div", "form-row");
  const proxyUrlLabel = node("label", "", "自定义代理地址");
  const proxyUrlInput = node("input", "wide-input");
  proxyUrlInput.type = "text";
  proxyUrlInput.placeholder = "例如 http://127.0.0.1:7890";
  proxyUrlInput.value = initial.proxy_url || "";
  proxyUrlRow.append(proxyUrlLabel, proxyUrlInput);

  const toggleRow = node("div", "agy-toggle-row");
  const toolsCheck = node("input");
  toolsCheck.type = "checkbox";
  toolsCheck.checked = initial.enable_tools !== false;
  const toolsWrap = node("label");
  toolsWrap.append(toolsCheck, node("span", "", "启用 MCP 工具调用"));

  const imagesCheck = node("input");
  imagesCheck.type = "checkbox";
  imagesCheck.checked = initial.enable_images !== false;
  const imagesWrap = node("label");
  imagesWrap.append(imagesCheck, node("span", "", "启用多模态图片输入"));
  toggleRow.append(toolsWrap, imagesWrap);

  configCard.append(cmdRow, thoughtRow, proxyRow, proxyUrlRow, toggleRow);

  // Login authorization code section
  const loginSection = node("div", "provider-login");
  const codeControls = node("div", "agy-code-controls");
  const codeInput = node("input", "wide-input");
  codeInput.type = "text";
  codeInput.placeholder = "粘贴浏览器显示的单行授权码";
  codeInput.setAttribute("aria-label", "授权码");
  const submitCodeButton = node("button", "primary-button", "提交授权码");
  submitCodeButton.type = "button";
  const openLoginButton = node("button", "secondary-button", "打开登录页面");
  openLoginButton.type = "button";
  codeControls.append(codeInput, submitCodeButton, openLoginButton);
  loginSection.append(
    node("p", "detail-desc", "在浏览器完成 Google 账号授权后，复制授权码并在此提交。"),
    codeControls,
  );

  // Test model card
  const testCard = node("section", "agy-account-card");
  const testControls = node("div", "agy-test-controls");
  const testHint = node("p", "detail-desc", "发送一条简短请求验证连通性，不更改对话设置。");
  const testModel = node("select");
  testModel.setAttribute("aria-label", "测试使用的模型");

  const actions = node("div", "detail-actions");

  let state = initial;
  let pending = null;
  let revision = 0;
  let busy = false;
  let disposed = false;
  let modelSnapshot = "";
  const messages = {
    login: "正在启动 agy 登录流程…",
    sync_models: "正在通过 agy acp 检测并同步模型…",
    test_connection: "正在测试模型…",
  };
  const current = () => !disposed && !signal.aborted && isCurrent();
  const report = error => {
    if (current()) onError(error);
  };
  const invoke = (id, values = {}) =>
    action(id, { operationRequest: { profileId: state.profileId, ...values } });
  const button = (text, callback, className = "secondary-button") => {
    const element = node("button", className, text);
    element.type = "button";
    element.addEventListener("click", () => Promise.resolve().then(callback).catch(report));
    return element;
  };

  const loginButton = button(
    "登录 Antigravity",
    () => (pending?.operation === "login" ? cancel() : start("login")),
    "primary-button",
  );
  const syncButton = button("检测并同步模型", () =>
    pending?.operation === "sync_models" ? cancel() : start("sync_models"),
  );
  const logoutButton = button("重置状态", () => mutate("logout"));
  const removeButton = button("删除模型服务", () => mutate("remove"), "danger-button");
  const testModelButton = button("测试模型", () =>
    pending?.operation === "test_connection"
      ? cancel()
      : start("test_connection", { modelId: testModel.value }),
  );
  const copyDetails = button(
    "复制详情",
    async () => {
      const ticket = revision;
      await window.navigator.clipboard.writeText(
        [status.textContent, reply.textContent].filter(Boolean).join("\n\n"),
      );
      if (current() && ticket === revision) copyDetails.textContent = "已复制";
    },
    "secondary-button compact-button agy-copy-details",
  );

  openLoginButton.addEventListener("click", () => {
    void invoke("openLogin").catch(report);
  });
  submitCodeButton.addEventListener("click", () => {
    const code = codeInput.value.trim();
    if (!code || !pending) return;
    submitCodeButton.disabled = true;
    void invoke("submitCode", { requestId: pending.requestId, code })
      .then(result => {
        if (current()) {
          codeInput.value = "";
          apply(result);
        }
      })
      .catch(error => {
        submitCodeButton.disabled = false;
        report(error);
      });
  });

  async function saveConfigPatch() {
    const ticket = revision;
    try {
      const result = await invoke("configure", {
        command: cmdInput.value.trim(),
        thought_level: thoughtSelect.value,
        proxy_mode: proxySelect.value,
        proxy_url: proxyUrlInput.value.trim(),
        enable_tools: toolsCheck.checked,
        enable_images: imagesCheck.checked,
      });
      if (current() && ticket === revision) apply(result);
    } catch (error) {
      report(error);
    }
  }

  name.addEventListener("change", async () => {
    const value = name.value;
    const ticket = revision;
    try {
      const result = await invoke("rename", { label: value });
      if (current() && ticket === revision && name.value === value) apply(result);
    } catch (error) {
      report(error);
    }
  });
  cmdInput.addEventListener("change", () => void saveConfigPatch());
  thoughtSelect.addEventListener("change", () => void saveConfigPatch());
  proxySelect.addEventListener("change", () => {
    proxyUrlRow.hidden = proxySelect.value !== "custom";
    if (proxySelect.value !== "custom" || proxyUrlInput.value.trim()) {
      void saveConfigPatch();
    }
  });
  proxyUrlInput.addEventListener("change", () => void saveConfigPatch());
  toolsCheck.addEventListener("change", () => void saveConfigPatch());
  imagesCheck.addEventListener("change", () => void saveConfigPatch());

  testModel.addEventListener("change", () => {
    if (pending?.operation === "test_connection") {
      void cancel().catch(report);
    } else if (state.operationResult?.operation === "test_connection") {
      state = { ...state, operationResult: {} };
      render();
    }
  });

  actions.append(loginButton, syncButton, logoutButton, removeButton);
  testControls.append(testModel, testModelButton);
  testCard.append(node("h3", "", "测试模型"), testControls, testHint);
  feedback.append(status, reply, copyDetails);
  generalFeedback.append(feedback);
  panel.append(
    heading,
    nameRow,
    accountLabel,
    hint,
    configCard,
    generalFeedback,
    loginSection,
    testCard,
    actions,
  );
  enhanceSelect(thoughtSelect);
  enhanceSelect(proxySelect);
  enhanceSelect(testModel);

  function render() {
    const loggedIn = Boolean(state.loggedIn);
    const result = state.operationResult || {};
    const blockingOperation = pending?.operation;
    panel.setAttribute("aria-busy", String(Boolean(pending) || busy));
    title.textContent = state.label;

    const caps = state.capabilities?.promptCapabilities || {};
    const capNotes = [
      state.cliVersion ? `agy ${state.cliVersion}` : "",
      caps.image ? "支持识图" : "",
      state.poolStats ? `活跃会话 ${state.poolStats.activeSessions}/${state.poolStats.maxSessions}` : "",
    ].filter(Boolean);
    accountLabel.hidden = !loggedIn && !capNotes.length;
    accountLabel.textContent = capNotes.join(" · ") || "已连接 agy CLI";

    hint.textContent = loggedIn
      ? (state.models?.length
        ? `已同步 ${state.models.length} 个 Antigravity 模型，可在“模型”页选用。`
        : "CLI 已就绪，点击“检测并同步模型”获取目录。")
      : "使用本机官方 agy 客户端连接；若终端已登录，可直接点击“检测并同步模型”。";

    proxyUrlRow.hidden = proxySelect.value !== "custom";

    const waitingCode = Boolean(pending?.operation === "login" && result.loginUrl);
    loginSection.hidden = !waitingCode;
    submitCodeButton.disabled = !waitingCode;

    const timing =
      result.operation === "test_connection" && result.elapsedMs != null
        ? `耗时 ${(result.elapsedMs / 1000).toFixed(2)} 秒`
        : "";
    status.textContent = [
      result.modelId,
      result.message || (pending ? messages[pending.operation] : ""),
      timing,
    ]
      .filter(Boolean)
      .join(" · ");
    status.hidden = !status.textContent;
    status.classList.toggle("error", result.state === "failed");
    reply.textContent = result.reply || "";
    reply.hidden = !reply.textContent;
    copyDetails.hidden =
      result.state !== "failed" &&
      !(result.operation === "test_connection" && result.state === "completed");

    const operation = pending?.operation || result.operation;
    const target = operation === "test_connection" ? testCard : generalFeedback;
    if (feedback.parentElement !== target) {
      target.insertBefore(feedback, target === testCard ? testHint : null);
    }
    feedback.hidden = status.hidden && reply.hidden && copyDetails.hidden;
    generalFeedback.hidden = target !== generalFeedback || feedback.hidden;

    loginButton.disabled = busy || (Boolean(blockingOperation) && blockingOperation !== "login");
    loginButton.textContent =
      pending?.operation === "login" ? "取消登录" : "登录 Antigravity";
    syncButton.disabled =
      busy || (Boolean(blockingOperation) && blockingOperation !== "sync_models");
    syncButton.textContent =
      pending?.operation === "sync_models" ? "取消检测" : "检测并同步模型";
    logoutButton.hidden = !loggedIn;
    logoutButton.disabled = busy || Boolean(blockingOperation);
    removeButton.disabled = busy;
    testModelButton.textContent =
      pending?.operation === "test_connection" ? "取消测试" : "测试模型";
    testModelButton.disabled =
      busy ||
      !(state.models || []).length ||
      (Boolean(blockingOperation) && blockingOperation !== "test_connection");
    testCard.hidden = !(state.models || []).length;

    const nextModels = JSON.stringify(state.models || []);
    if (modelSnapshot !== nextModels) {
      modelSnapshot = nextModels;
      const selected = testModel.value;
      testModel.replaceChildren();
      for (const model of state.models || []) {
        const option = node("option", "", model.label || model.modelId);
        option.value = model.modelId;
        testModel.append(option);
      }
      if (!(state.models || []).length) {
        testModel.append(node("option", "", "暂无可用模型"));
      }
      testModel.value = (state.models || []).some(model => model.modelId === selected)
        ? selected
        : state.models?.[0]?.modelId || "";
    }
    testModel.disabled = !(state.models || []).length;
    refreshSelect(thoughtSelect);
    refreshSelect(proxySelect);
    refreshSelect(testModel);
  }

  async function mutate(id) {
    if (pending) void cancel().catch(report);
    const ticket = ++revision;
    busy = true;
    render();
    try {
      const result = await invoke(id);
      if (current() && ticket === revision) apply(result);
    } finally {
      busy = false;
      if (current()) render();
    }
  }

  async function cancel() {
    closeSelects(panel);
    const previous = pending;
    pending = null;
    revision++;
    state = { ...state, operationResult: {} };
    if (current()) render();
    if (previous) await invoke("cancel", previous);
  }

  async function start(operation, values = {}) {
    if (busy || pending) return;
    const request = { ...values, operation, requestId: window.crypto.randomUUID() };
    pending = request;
    const ticket = ++revision;
    let opened = false;
    let terminal = false;
    copyDetails.textContent = "复制详情";
    state = { ...state, operationResult: { operation, message: messages[operation] } };
    render();
    try {
      let result = await invoke("start", request);
      while (current() && ticket === revision) {
        apply(result);
        const value = state.operationResult;
        if (value?.requestId === request.requestId) {
          if (value.loginUrl && !opened) {
            opened = true;
            void invoke("openLogin").catch(error => {
              if (ticket === revision) report(error);
            });
          }
          terminal = ["failed", "completed", "cancelled"].includes(value.state);
          if (
            value.state === "failed" &&
            (operation === "login" || operation === "sync_models")
          ) {
            report(new Error(value.message, { cause: value }));
          }
          if (terminal) break;
        }
        await new Promise(resolve => window.setTimeout(resolve, 250));
        if (!current() || ticket !== revision) return;
        result = await invoke("status", request);
      }
    } catch (error) {
      if (current() && ticket === revision) {
        state = {
          ...state,
          operationResult: {
            ...state.operationResult,
            operation,
            state: "failed",
            message: error.message,
          },
        };
        if (operation === "login" || operation === "sync_models") report(error);
      }
    } finally {
      if (pending === request) {
        pending = null;
        if (current()) {
          render();
          if (operation === "test_connection" && terminal) {
            feedback.scrollIntoView({ block: "nearest" });
          }
        }
        if (!terminal) await invoke("cancel", request).catch(report);
      }
    }
  }

  render();
  return {
    element: panel,
    cancel,
    value: () => state,
    update(value) {
      if (name.value === state.label) name.value = value.label;
      if (cmdInput.value === (state.command || "")) cmdInput.value = value.command || "";
      thoughtSelect.value = value.thought_level || "default";
      proxySelect.value = value.proxy_mode || "inherit";
      if (proxyUrlInput.value === (state.proxy_url || "")) {
        proxyUrlInput.value = value.proxy_url || "";
      }
      toolsCheck.checked = value.enable_tools !== false;
      imagesCheck.checked = value.enable_images !== false;
      state = value;
      render();
    },
    async select() {
      const ticket = ++revision;
      const result = await invoke("status");
      if (current() && ticket === revision) apply(result);
    },
    dispose() {
      disposed = true;
      void cancel().catch(report);
      panel.remove();
    },
  };
}
