const LANGUAGE_OPTIONS = [
  { code: "eng", label: "English" },
  { code: "spa", label: "Español" },
  { code: "fra", label: "Français" },
  { code: "hin", label: "हिन्दी" },
  { code: "kor", label: "한국어" },
  { code: "cmn", label: "中文" },
  { code: "jpn", label: "日本語" },
];

const DEFAULT_WELCOME_MESSAGES = {
  eng: "Welcome In! Here to help with any questions you may have about:\n\n- Recommendations\n- Menu Items\n- Operations\n\nWhat could I help you with today?",
  spa: "¡Bienvenido! Estoy aqui para ayudarte con cualquier pregunta que tengas sobre:\n\n- Recomendaciones\n- Platos del menu\n- Operaciones\n\n¿Con que puedo ayudarte hoy?",
  fra: "Bienvenue ! Je suis la pour vous aider avec toutes vos questions sur :\n\n- Recommandations\n- Articles du menu\n- Operations\n\nComment puis-je vous aider aujourd'hui ?",
  hin: "स्वागत है! मैं इन विषयों पर आपके किसी भी प्रश्न में मदद के लिए यहां हूं:\n\n- सुझाव\n- मेनू आइटम\n- संचालन\n\nआज मैं आपकी कैसे मदद कर सकता हूं?",
  kor: "환영합니다! 다음에 관한 질문이 있으면 도와드릴게요:\n\n- 추천\n- 메뉴 항목\n- 운영 정보\n\n오늘 무엇을 도와드릴까요?",
  cmn: "欢迎！我可以帮助解答您关于以下内容的任何问题：\n\n- 推荐\n- 菜单项目\n- 营业信息\n\n今天我可以帮您什么？",
  jpn: "ようこそ！以下についてのご質問をお手伝いします：\n\n- おすすめ\n- メニュー項目\n- 営業情報\n\n今日はどのようにお手伝いできますか？",
};

const DEFAULT_UI_COPY = {
  eng: {
    languageLabel: "Language:",
    placeholder: "Ask anything",
    sendLabel: "Ask",
    disclaimer: "AI can make mistakes. Check important info.",
    waitingPhrases: ["Preparing response...", "Searching for information...", "Thinking...", "Just a second..."],
  },
  spa: {
    languageLabel: "Idioma:",
    placeholder: "Pregunta lo que quieras",
    sendLabel: "Preguntar",
    disclaimer: "La IA puede cometer errores. Verifica la informacion importante.",
    waitingPhrases: ["Preparando respuesta...", "Buscando informacion...", "Pensando...", "Un momento..."],
  },
  fra: {
    languageLabel: "Langue :",
    placeholder: "Posez votre question",
    sendLabel: "Demander",
    disclaimer: "L'IA peut faire des erreurs. Verifiez les informations importantes.",
    waitingPhrases: ["Preparation de la reponse...", "Recherche d'informations...", "Reflexion...", "Un instant..."],
  },
  hin: {
    languageLabel: "भाषा:",
    placeholder: "कुछ भी पूछें",
    sendLabel: "पूछें",
    disclaimer: "AI गलतियां कर सकता है। महत्वपूर्ण जानकारी जांच लें।",
    waitingPhrases: ["उत्तर तैयार हो रहा है...", "जानकारी खोजी जा रही है...", "सोच रहा हूं...", "बस एक क्षण..."],
  },
  kor: {
    languageLabel: "언어:",
    placeholder: "무엇이든 물어보세요",
    sendLabel: "질문",
    disclaimer: "AI는 실수할 수 있습니다. 중요한 정보는 확인하세요.",
    waitingPhrases: ["응답을 준비하고 있어요...", "정보를 찾고 있어요...", "생각 중...", "잠시만요..."],
  },
  cmn: {
    languageLabel: "语言：",
    placeholder: "随便问",
    sendLabel: "提问",
    disclaimer: "AI 可能会出错。请核对重要信息。",
    waitingPhrases: ["正在准备回复...", "正在查找信息...", "正在思考...", "请稍等..."],
  },
  jpn: {
    languageLabel: "言語:",
    placeholder: "何でも聞いてください",
    sendLabel: "質問",
    disclaimer: "AI は間違えることがあります。重要な情報は確認してください。",
    waitingPhrases: ["回答を準備しています...", "情報を検索しています...", "考えています...", "少々お待ちください..."],
  },
};

class RestaurantChatWidget extends HTMLElement {
  static get observedAttributes() {
    return [
      "api-base-url",
      "restaurant-id",
      "title",
      "subtitle",
      "layout",
      "placeholder",
      "accent-color",
      "send-label",
      "welcome-message",
    ];
  }

  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.state = {
      widgetToken: "",
      widgetTokenExpiresAt: 0,
      sessionToken: "",
      newSessionPending: true,
      selectedLanguageCode: "eng",
      tokenLoading: false,
      sending: false,
      assistantBuffer: "",
      assistantImages: [],
      thinkingDots: 0,
      thinkingTimer: null,
      thinkingTypeTimer: null,
      thinkingStartTimer: null,
    };
    this.$ = {};
  }

  connectedCallback() {
    this.startFreshSession();
    this.render();
    this.bindEvents();
    this.appendMessage("assistant", this.getWelcomeMessage());
    this.ensureWidgetToken().catch((err) => this.showError(err.message));
  }

  attributeChangedCallback(name, oldValue, newValue) {
    if (name === "restaurant-id" && oldValue !== newValue) {
      this.startFreshSession();
    }
    this.render();
    this.bindEvents();
  }

  getConfig() {
    const attr = (key, fallback = "") => this.getAttribute(key) || fallback;
    return {
      apiBaseUrl: attr("api-base-url").replace(/\/+$/, ""),
      restaurantId: attr("restaurant-id"),
      title: attr("title", "Ask Our Menu AI"),
      subtitle: attr("subtitle", "Live menu answers with source-backed context"),
      layout: attr("layout", "embedded"),
      placeholder: attr("placeholder", "Ask about dishes, prices, allergies, and specials..."),
      accentColor: attr("accent-color", "#0f766e"),
      sendLabel: attr("send-label", "Send"),
      welcomeMessage: attr("welcome-message", ""),
    };
  }

  getWelcomeMessage() {
    const selectedLanguageCode = this.state.selectedLanguageCode ?? "eng";
    return this.getConfig().welcomeMessage || DEFAULT_WELCOME_MESSAGES[selectedLanguageCode] || DEFAULT_WELCOME_MESSAGES.eng;
  }

  getUiCopy() {
    const selectedLanguageCode = this.state.selectedLanguageCode ?? "eng";
    return DEFAULT_UI_COPY[selectedLanguageCode] || DEFAULT_UI_COPY.eng;
  }

  render() {
    const config = this.getConfig();
    const isFullscreen = config.layout === "fullscreen";
    const uiCopy = this.getUiCopy();
    const selectedLanguageCode = this.state.selectedLanguageCode ?? "eng";
    const languageOptions = LANGUAGE_OPTIONS.map(({ code, label }) => {
      const selected = code === selectedLanguageCode ? " selected" : "";
      return `<option value="${code}"${selected}>${this.escapeHtml(label)}</option>`;
    }).join("");

    this.shadowRoot.innerHTML = `
      <style>
        :host {
          --ink: #202123;
          --muted: #6e6e80;
          --surface: #ffffff;
          --stroke: #e5e5e8;
          --user-bubble: #f1f1f3;
          --accent: ${config.accentColor};
          display: block;
          width: ${isFullscreen ? "100%" : "min(100%, 430px)"};
          height: ${isFullscreen ? "100%" : "auto"};
          min-height: ${isFullscreen ? "100%" : "auto"};
          font-family: "Inter", "Segoe UI", system-ui, sans-serif;
          color: var(--ink);
          background: var(--surface);
        }

        .shell {
          border: ${isFullscreen ? "0" : "1px solid var(--stroke)"};
          border-radius: ${isFullscreen ? "0" : "18px"};
          overflow: hidden;
          background: var(--surface);
          box-shadow: ${isFullscreen ? "none" : "0 8px 24px rgba(0, 0, 0, 0.08)"};
          display: flex;
          flex-direction: column;
          height: ${isFullscreen ? "100vh" : "auto"};
          height: ${isFullscreen ? "100dvh" : "auto"};
          position: relative;
        }

        .token-loading {
          position: absolute;
          inset: 0;
          display: flex;
          flex-direction: column;
          align-items: center;
          justify-content: center;
          gap: 14px;
          padding: 24px;
          background: rgba(255, 255, 255, 0.92);
          backdrop-filter: blur(8px);
          z-index: 10;
          opacity: 0;
          pointer-events: none;
          transition: opacity 160ms ease;
        }

        .token-loading.visible {
          opacity: 1;
          pointer-events: auto;
        }

        .token-spinner {
          width: 36px;
          height: 36px;
          border-radius: 999px;
          border: 3px solid #e5e7eb;
          border-top-color: var(--accent);
          animation: spin 0.9s linear infinite;
        }

        .token-loading-title {
          margin: 0;
          font-size: 16px;
          font-weight: 600;
          color: var(--ink);
        }

        .token-loading-copy {
          margin: 0;
          max-width: 280px;
          text-align: center;
          font-size: 13px;
          line-height: 1.5;
          color: var(--muted);
        }

        .messages {
          padding: 20px 16px 14px;
          flex: 1 1 auto;
          min-height: 0;
          height: ${isFullscreen ? "auto" : "62vh"};
          max-height: ${isFullscreen ? "none" : "none"};
          min-height: ${isFullscreen ? "0" : "420px"};
          overflow-y: auto;
          overflow-x: hidden;
          display: flex;
          flex-direction: column;
          gap: 20px;
        }

        .bubble {
          max-width: 88%;
          padding: 12px 14px;
          border-radius: 18px;
          line-height: 1.55;
          font-size: 17px;
          white-space: pre-wrap;
          word-wrap: break-word;
          animation: rise 140ms ease-out;
        }

        .assistant {
          align-self: flex-start;
          max-width: 100%;
          padding: 0;
          border: 0;
          border-radius: 0;
          background: transparent;
        }

        .user {
          align-self: flex-end;
          color: #202123;
          background: var(--user-bubble);
        }

        .error {
          align-self: center;
          color: #991b1b;
          font-size: 12px;
          background: #fee2e2;
          border: 1px solid #fca5a5;
          border-radius: 8px;
          padding: 8px 10px;
        }

        .bubble.thinking {
          color: #6b7280;
          font-style: normal;
        }

        .thinking-cursor {
          display: inline-block;
          width: 3px;
          height: 1.05em;
          margin-left: 2px;
          background: currentColor;
          vertical-align: -0.15em;
          animation: cursor-blink 1s steps(2, start) infinite;
        }

        .assistant .md-image {
          display: block;
          width: 100%;
          max-width: 280px;
          border-radius: 10px;
          margin: 8px 0;
          border: 1px solid #e7e5e4;
          background: #e5e7eb;
        }

        .assistant .image-carousel {
          margin-top: 10px;
          padding-top: 8px;
          border-top: 1px dashed #d6d3d1;
        }

        .assistant .image-controls {
          display: flex;
          justify-content: center;
          gap: 6px;
          margin-top: 8px;
        }

        .assistant .image-nav {
          border: 1px solid #d6d3d1;
          background: #ffffff;
          color: #374151;
          border-radius: 999px;
          width: 28px;
          height: 28px;
          line-height: 1;
          cursor: pointer;
        }

        .assistant .image-nav[disabled] {
          opacity: 0.45;
          cursor: not-allowed;
        }

        .assistant .image-track {
          display: flex;
          gap: 0;
          overflow-x: auto;
          scroll-snap-type: x mandatory;
          -webkit-overflow-scrolling: touch;
          scrollbar-width: thin;
          padding-bottom: 2px;
        }

        .assistant .image-slide {
          min-width: 100%;
          width: 100%;
          flex: 0 0 100%;
          scroll-snap-align: start;
          border: 1px solid #e7e5e4;
          border-radius: 10px;
          overflow: hidden;
          background: #ffffff;
        }

        .assistant .image-slide-img {
          width: 100%;
          height: 150px;
          object-fit: cover;
          display: block;
          background: #e5e7eb;
        }

        .assistant .image-slide-title {
          margin: 0;
          padding: 6px 8px 8px;
          font-size: 12px;
          font-weight: 700;
          line-height: 1.25;
          color: #1f2937;
        }

        .composer {
          display: grid;
          gap: 8px;
          border-top: 1px solid var(--stroke);
          padding: 10px 12px 8px;
          background: #ffffff;
          flex: 0 0 auto;
        }

        .language-row {
          display: flex;
          justify-content: flex-end;
          align-items: center;
          gap: 8px;
        }

        .field-label {
          display: inline;
          font-size: 12px;
          font-weight: 600;
          color: var(--muted);
        }

        .composer-main {
          display: grid;
          grid-template-columns: auto 1fr auto;
          gap: 8px;
          align-items: stretch;
          border: 1px solid var(--stroke);
          border-radius: 30px;
          padding: 6px;
          background: #ffffff;
        }

        select,
        textarea {
          resize: none;
          border: 1px solid #d6d3d1;
          border-radius: 10px;
          min-height: 44px;
          padding: 11px 12px;
          font: inherit;
          color: var(--ink);
          background: #fff;
          outline: none;
        }

        select {
          appearance: none;
          border-radius: 999px;
          min-height: 28px;
          padding: 4px 10px;
          font-size: 12px;
          color: var(--muted);
          border-color: transparent;
          background: #f7f7f8;
        }

        textarea {
          border: 0;
          min-height: 40px;
          max-height: 120px;
          padding: 9px 8px;
        }

        select:focus,
        textarea:focus {
          border-color: var(--accent);
          box-shadow: 0 0 0 2px color-mix(in srgb, var(--accent), white 75%);
        }

        button {
          border: 0;
          border-radius: 999px;
          min-width: 72px;
          padding: 0 14px;
          font: inherit;
          font-weight: 600;
          background: #f2f2f2;
          color: #202123;
          cursor: pointer;
        }

        .attach {
          width: 40px;
          min-width: 40px;
          padding: 0;
          font-size: 24px;
          line-height: 1;
          background: transparent;
        }

        .send {
          background: #f2f2f2;
        }

        .composer-note {
          margin: 0;
          text-align: center;
          font-size: 12px;
          color: var(--muted);
        }

        button[disabled] {
          opacity: 0.5;
          cursor: not-allowed;
        }

        @media (max-width: 560px) {
          .messages {
            height: ${isFullscreen ? "auto" : "64vh"};
            min-height: ${isFullscreen ? "0" : "420px"};
          }

          .composer-main {
            grid-template-columns: auto 1fr auto;
          }

          button {
            min-height: 44px;
          }
        }

        @keyframes rise {
          from {
            opacity: 0;
            transform: translateY(4px);
          }
          to {
            opacity: 1;
            transform: translateY(0);
          }
        }

        @keyframes spin {
          from {
            transform: rotate(0deg);
          }
          to {
            transform: rotate(360deg);
          }
        }

        @keyframes cursor-blink {
          0%, 45% {
            opacity: 1;
          }
          46%, 100% {
            opacity: 0;
          }
        }
      </style>

      <div class="shell">
        <div class="token-loading${this.state.tokenLoading ? " visible" : ""}" aria-live="polite" aria-busy="${this.state.tokenLoading ? "true" : "false"}">
          <div class="token-spinner" aria-hidden="true"></div>
          <p class="token-loading-title">Starting chat</p>
          <p class="token-loading-copy">We are securely connecting to the chat service and issuing your session token.</p>
        </div>
        <section class="messages" part="messages" aria-live="polite"></section>
        <form class="composer">
          <label class="language-row">
            <span class="field-label">${this.escapeHtml(uiCopy.languageLabel)}</span>
            <select aria-label="Select chat language">
              ${languageOptions}
            </select>
          </label>
          <div class="composer-main">
            <textarea placeholder="${this.escapeHtml(uiCopy.placeholder)}"></textarea>
            <button type="submit" class="send">${this.escapeHtml(uiCopy.sendLabel)}</button>
          </div>
          <p class="composer-note">${this.escapeHtml(uiCopy.disclaimer)}</p>
        </form>
      </div>
    `;

    this.$.messages = this.shadowRoot.querySelector(".messages");
    this.$.form = this.shadowRoot.querySelector("form");
    this.$.input = this.shadowRoot.querySelector("textarea");
    this.$.button = this.shadowRoot.querySelector("button.send");
    this.$.languageSelect = this.shadowRoot.querySelector("select");
    this.$.tokenLoading = this.shadowRoot.querySelector(".token-loading");
  }

  bindEvents() {
    if (!this.$.form || !this.$.input || !this.$.languageSelect) return;
    this.$.form.onsubmit = (event) => {
      event.preventDefault();
      const text = this.$.input.value.trim();
      if (!text || this.state.sending) return;
      this.$.input.value = "";
      this.sendMessage(text).catch((err) => this.showError(err.message));
    };
    this.$.languageSelect.onchange = (event) => {
      const nextLanguageCode = event.target.value || "eng";
      if (nextLanguageCode === this.state.selectedLanguageCode) return;
      this.state.selectedLanguageCode = nextLanguageCode;
      this.render();
      this.bindEvents();
      this.resetConversation();
    };
  }

  appendMessage(role, content) {
    if (!this.$.messages) return;
    const el = document.createElement("div");
    el.className = `bubble ${role}`;
    el.textContent = content;
    this.$.messages.appendChild(el);
    this.scrollToBottom();
    return el;
  }

  clearMessages() {
    if (!this.$.messages) return;
    this.$.messages.textContent = "";
  }

  setMessages(messages = []) {
    this.clearMessages();
    for (const message of messages) {
      if (!message || typeof message !== "object") continue;
      const role = message.role === "user" ? "user" : "assistant";
      const text = String(message.content || "");
      const bubble = this.appendMessage(role, text);
      if (role === "assistant") {
        this.renderAssistantContent(bubble, text, this.normalizeImagesEvent(message.images));
      }
    }
  }

  showError(text) {
    if (!this.$.messages) return;
    const el = document.createElement("div");
    el.className = "error";
    el.textContent = text;
    this.$.messages.appendChild(el);
    this.scrollToBottom();
  }

  setSending(next) {
    this.state.sending = next;
    if (this.$.button) this.$.button.disabled = next;
    if (this.$.input) this.$.input.disabled = next;
    if (this.$.languageSelect) this.$.languageSelect.disabled = next;
  }

  setTokenLoading(next) {
    this.state.tokenLoading = next;
    if (this.$.tokenLoading) {
      this.$.tokenLoading.classList.toggle("visible", next);
      this.$.tokenLoading.setAttribute("aria-busy", next ? "true" : "false");
    }
    if (this.$.button) this.$.button.disabled = next || this.state.sending;
    if (this.$.input) this.$.input.disabled = next || this.state.sending;
    if (this.$.languageSelect) this.$.languageSelect.disabled = next || this.state.sending;
  }

  startFreshSession() {
    this.state.sessionToken = "";
    this.state.newSessionPending = true;
  }

  persistSessionToken(sessionToken) {
    const token = String(sessionToken || "").trim();
    if (!token) return;
    this.state.sessionToken = token;
    this.state.newSessionPending = false;
  }

  resetConversation() {
    this.stopThinkingIndicator();
    this.startFreshSession();
    this.state.assistantBuffer = "";
    this.state.assistantImages = [];
    this.clearMessages();
    this.appendMessage("assistant", this.getWelcomeMessage());
  }

  scrollToBottom() {
    this.$.messages.scrollTop = this.$.messages.scrollHeight;
  }

  startThinkingIndicator(assistantBubble) {
    this.stopThinkingIndicator(assistantBubble);
    assistantBubble.classList.add("thinking");
    this.renderThinkingIndicator(assistantBubble, "");

    if (Math.random() >= 0.90) return;

    const phrases = this.getUiCopy().waitingPhrases || DEFAULT_UI_COPY.eng.waitingPhrases;
    const phrase = phrases[Math.floor(Math.random() * phrases.length)];
    this.state.thinkingStartTimer = setTimeout(() => {
      let index = 0;
      this.state.thinkingTypeTimer = setInterval(() => {
        index += 1;
        this.renderThinkingIndicator(assistantBubble, phrase.slice(0, index));
        this.scrollToBottom();
        if (index >= phrase.length) {
          clearInterval(this.state.thinkingTypeTimer);
          this.state.thinkingTypeTimer = null;
        }
      }, 50);
    }, 1200);
  }

  stopThinkingIndicator(assistantBubble) {
    if (this.state.thinkingTimer) {
      clearInterval(this.state.thinkingTimer);
      this.state.thinkingTimer = null;
    }
    if (this.state.thinkingTypeTimer) {
      clearInterval(this.state.thinkingTypeTimer);
      this.state.thinkingTypeTimer = null;
    }
    if (this.state.thinkingStartTimer) {
      clearTimeout(this.state.thinkingStartTimer);
      this.state.thinkingStartTimer = null;
    }
    if (assistantBubble) {
      assistantBubble.classList.remove("thinking");
    }
  }

  renderThinkingIndicator(assistantBubble, text) {
    if (!assistantBubble) return;
    assistantBubble.textContent = "";
    if (text) assistantBubble.appendChild(document.createTextNode(text));
    const cursor = document.createElement("span");
    cursor.className = "thinking-cursor";
    assistantBubble.appendChild(cursor);
  }

  async ensureWidgetToken(forceRefresh = false) {
    const { apiBaseUrl, restaurantId } = this.getConfig();
    if (!apiBaseUrl) throw new Error("Missing required attribute: api-base-url");
    if (!restaurantId) throw new Error("Missing required attribute: restaurant-id");

    const now = Math.floor(Date.now() / 1000);
    const hasFreshToken =
      this.state.widgetToken && this.state.widgetTokenExpiresAt && this.state.widgetTokenExpiresAt > now + 20;

    if (!forceRefresh && hasFreshToken) {
      return this.state.widgetToken;
    }

    this.setTokenLoading(true);
    try {
      const response = await fetch(`${apiBaseUrl}/api/widget-token`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ restaurantId }),
      });

      if (!response.ok) {
        const errorBody = await this.safeReadError(response);
        throw new Error(errorBody || `Widget token request failed (${response.status})`);
      }

      const data = await response.json();
      if (!data.widgetToken) throw new Error("Token response missing widgetToken");
      this.state.widgetToken = data.widgetToken;
      this.state.widgetTokenExpiresAt = Number(data.expiresAt || 0);
      return this.state.widgetToken;
    } finally {
      this.setTokenLoading(false);
    }
  }

  async sendMessage(message, allowRetry = true) {
    const { apiBaseUrl, restaurantId } = this.getConfig();
    if (!apiBaseUrl) throw new Error("Missing required attribute: api-base-url");
    if (!restaurantId) throw new Error("Missing required attribute: restaurant-id");

    this.setSending(true);
    this.appendMessage("user", message);
    const assistantBubble = this.appendMessage("assistant", "");
    this.startThinkingIndicator(assistantBubble);
    this.state.assistantBuffer = "";
    this.state.assistantImages = [];

    try {
      await this.ensureWidgetToken();
      const payload = {
        message,
        restaurantId,
        widgetToken: this.state.widgetToken,
        language: this.state.selectedLanguageCode ?? "eng",
      };
      if (this.state.newSessionPending || !this.state.sessionToken) {
        payload.newSession = true;
      } else {
        payload.sessionToken = this.state.sessionToken;
      }

      const response = await fetch(`${apiBaseUrl}/api/chat-stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });

      if (!response.ok || !response.body) {
        if (response.status === 403 && allowRetry) {
          await this.ensureWidgetToken(true);
          this.stopThinkingIndicator(assistantBubble);
          assistantBubble.remove();
          this.setSending(false);
          return this.sendMessage(message, false);
        }
        const err = await this.safeReadError(response);
        throw new Error(err || `Chat request failed (${response.status})`);
      }

      await this.consumeNdjsonStream(response.body, assistantBubble);
      if (!this.state.assistantBuffer.trim()) {
        assistantBubble.textContent = "No response returned.";
      }
    } finally {
      this.stopThinkingIndicator(assistantBubble);
      this.setSending(false);
    }
  }

  async consumeNdjsonStream(stream, assistantBubble) {
    const reader = stream.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";

      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed) continue;
        this.applyStreamEvent(trimmed, assistantBubble);
      }
    }

    const tail = buffer.trim();
    if (tail) this.applyStreamEvent(tail, assistantBubble);
  }

  applyStreamEvent(line, assistantBubble) {
    let evt;
    try {
      evt = JSON.parse(line);
    } catch (_) {
      return;
    }

    if (evt.type === "session" && evt.sessionToken) {
      this.persistSessionToken(evt.sessionToken);
      return;
    }

    if (evt.type === "images") {
      const normalized = this.normalizeImagesEvent(evt.images);
      if (normalized.length > 0) {
        this.state.assistantImages = normalized;
        this.renderAssistantContent(assistantBubble, this.state.assistantBuffer, this.state.assistantImages);
        this.scrollToBottom();
      }
      return;
    }

    if (evt.type === "delta" && typeof evt.content === "string") {
      this.stopThinkingIndicator(assistantBubble);
      this.state.assistantBuffer += evt.content;
      this.renderAssistantContent(assistantBubble, this.state.assistantBuffer, this.state.assistantImages);
      this.scrollToBottom();
      return;
    }

    if (evt.type === "done") {
      if (evt.message && typeof evt.message.content === "string" && !this.state.assistantBuffer) {
        this.state.assistantBuffer = evt.message.content;
        this.renderAssistantContent(assistantBubble, this.state.assistantBuffer, this.state.assistantImages);
        this.scrollToBottom();
      }
      this.stopThinkingIndicator(assistantBubble);
      return;
    }

    if (evt.type === "error") {
      this.stopThinkingIndicator(assistantBubble);
      const message = evt.message || "Streaming error";
      this.state.assistantBuffer += `${this.state.assistantBuffer ? "\n\n" : ""}${message}`;
      this.renderAssistantContent(assistantBubble, this.state.assistantBuffer, this.state.assistantImages);
      this.scrollToBottom();
    }
  }

  normalizeImagesEvent(images) {
    if (!Array.isArray(images)) return [];

    const unique = new Map();
    for (const item of images) {
      if (!item || typeof item !== "object") continue;
      const imageUrl = String(item.image_url || "").trim();
      if (!/^https?:\/\//i.test(imageUrl)) continue;
      if (unique.has(imageUrl)) continue;
      unique.set(imageUrl, {
        image_url: imageUrl,
        title: String(item.title || "Menu item").trim() || "Menu item",
      });
      if (unique.size >= 4) break;
    }
    return Array.from(unique.values());
  }

  renderAssistantContent(assistantBubble, text, images = []) {
    if (!assistantBubble) return;
    const value = String(text || "");
    assistantBubble.textContent = "";

    const pattern = /!\[([^\]]*)\]\((https?:\/\/[^\s)]+)\)/g;
    let index = 0;
    let match;

    while ((match = pattern.exec(value)) !== null) {
      const before = value.slice(index, match.index);
      if (before) assistantBubble.appendChild(document.createTextNode(before));

      const alt = match[1] || "Image";
      const src = match[2] || "";
      if (/^https?:\/\//i.test(src)) {
        const img = document.createElement("img");
        img.className = "md-image";
        img.loading = "lazy";
        img.alt = alt;
        img.src = src;
        assistantBubble.appendChild(img);
      } else {
        assistantBubble.appendChild(document.createTextNode(match[0]));
      }

      index = pattern.lastIndex;
    }

    const tail = value.slice(index);
    if (tail) assistantBubble.appendChild(document.createTextNode(tail));

    if (Array.isArray(images) && images.length > 0) {
      const carousel = document.createElement("div");
      carousel.className = "image-carousel";

      const controls = document.createElement("div");
      controls.className = "image-controls";

      const prevBtn = document.createElement("button");
      prevBtn.type = "button";
      prevBtn.className = "image-nav";
      prevBtn.textContent = "‹";

      const nextBtn = document.createElement("button");
      nextBtn.type = "button";
      nextBtn.className = "image-nav";
      nextBtn.textContent = "›";

      const track = document.createElement("div");
      track.className = "image-track";

      for (const image of images) {
        const slide = document.createElement("div");
        slide.className = "image-slide";

        const img = document.createElement("img");
        img.className = "image-slide-img";
        img.loading = "lazy";
        img.src = image.image_url;
        img.alt = image.title || "Menu item";
        slide.appendChild(img);

        const title = document.createElement("p");
        title.className = "image-slide-title";
        title.textContent = image.title || "Menu item";
        slide.appendChild(title);

        track.appendChild(slide);
      }

      const step = () => Math.max(180, track.clientWidth * 0.88);
      prevBtn.onclick = () => track.scrollBy({ left: -step(), behavior: "smooth" });
      nextBtn.onclick = () => track.scrollBy({ left: step(), behavior: "smooth" });

      if (images.length === 1) {
        prevBtn.disabled = true;
        nextBtn.disabled = true;
      }

      carousel.appendChild(track);
      controls.appendChild(prevBtn);
      controls.appendChild(nextBtn);
      carousel.appendChild(controls);
      assistantBubble.appendChild(carousel);
    }
  }

  async safeReadError(response) {
    try {
      const text = await response.text();
      if (!text) return "";
      const parsed = JSON.parse(text);
      if (parsed && typeof parsed.error === "string") return parsed.error;
      return text;
    } catch (_) {
      return "";
    }
  }

  escapeHtml(value) {
    return String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }
}

if (!customElements.get("restaurant-chat-widget")) {
  customElements.define("restaurant-chat-widget", RestaurantChatWidget);
}

export { RestaurantChatWidget };
