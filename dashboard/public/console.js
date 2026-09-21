(() => {
  "use strict";

  const COMPONENTS = [
    {
      id: "orchestrator",
      suffix: "runtime-orchestrator",
      name: "Orchestrator",
      description: "Customer-facing runtime for coordinated agent tasks.",
    },
    {
      id: "code-agent",
      suffix: "runtime-code-agent",
      name: "Code agent",
      description: "A2A specialist runtime for code-oriented work.",
    },
    {
      id: "research-agent",
      suffix: "runtime-research-agent",
      name: "Research agent",
      description: "A2A specialist runtime for research-oriented work.",
    },
  ];

  const state = {
    agents: [],
    interactive: false,
    healthChecked: false,
    busy: false,
    sessionId: "",
    csrfToken: "",
    statusAvailable: false,
  };

  const byId = (id) => document.getElementById(id);
  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };

  function sessionId() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      return `session-${window.crypto.randomUUID().replaceAll("-", "")}`;
    }
    const random = Math.random().toString(36).slice(2).padEnd(32, "0");
    return `session-${Date.now()}-${random}`;
  }

  function protocolFor(component, data) {
    if (component !== "orchestrator") return "A2A · IAM";
    if (data?.migration_runtime) return "HTTP · migration";
    return String(data?.agent_pattern || "").startsWith("agui-")
      ? "AG-UI · OAuth"
      : "HTTP · OAuth";
  }

  function agentsFromStatus(data) {
    if (!data || !data.stacks || typeof data.stacks !== "object") return [];
    const entries = Object.entries(data.stacks);
    return COMPONENTS.map((component) => {
      const entry = entries.find(([name]) => name.endsWith(`-${component.suffix}`));
      const stack = entry?.[1];
      if (!stack || stack.state === "not-applicable") return null;
      return {
        ...component,
        state: stack.state || "not-deployed",
        status: stack.status || "NOT_DEPLOYED",
        runtimeId: stack.outputs?.RuntimeId || "",
        protocol: protocolFor(component.id, data),
        customerFacing: component.id === "orchestrator",
      };
    }).filter(Boolean);
  }

  function statusBadge(agent) {
    const labels = {
      deployed: "Deployed",
      "in-progress": "Deploying",
      failed: "Failed",
      "not-deployed": "Not deployed",
    };
    const badge = el("span", `agent-status agent-status--${agent.state}`);
    badge.append(el("span", "agent-status-dot"));
    badge.append(document.createTextNode(labels[agent.state] || agent.state));
    return badge;
  }

  function renderAgents() {
    const grid = byId("agentGrid");
    const empty = byId("agentsEmpty");
    if (!grid || !empty) return;
    grid.replaceChildren();
    const deployed = state.agents.filter((agent) => agent.state === "deployed");
    byId("agentNavCount").textContent = String(deployed.length);
    empty.hidden = state.agents.length > 0;
    byId("agentsEmptyTitle").textContent = state.statusAvailable
      ? "No deployed runtimes"
      : "Runtime status unavailable";
    byId("agentsEmptyMessage").textContent = state.statusAvailable
      ? "Deploy the orchestrator runtime, then wait for the monitor to publish a fresh successful status."
      : "Start or repair the AWS monitor before using the runtime inventory.";

    state.agents.forEach((agent) => {
      const card = el("article", "agent-card");
      const head = el("div", "agent-card-head");
      const icon = el("div", "agent-icon", "AC");
      const title = el("div", "agent-title");
      title.append(el("h3", "", agent.name));
      title.append(el("p", "", agent.description));
      head.append(icon, title, statusBadge(agent));

      const facts = el("dl", "agent-facts");
      const fact = (label, value, mono = false) => {
        const row = el("div");
        row.append(el("dt", "", label), el("dd", mono ? "mono" : "", value || "—"));
        facts.append(row);
      };
      fact("Component", agent.id, true);
      fact("Protocol", agent.protocol);
      fact("Runtime ID", agent.runtimeId, true);

      const foot = el("div", "agent-card-foot");
      const type = el(
        "span",
        "badge badge--agent",
        agent.customerFacing ? "Customer entry point" : "Internal A2A runtime",
      );
      const button = el(
        "button",
        "console-button console-button--outline",
        agent.customerFacing ? "Try agent" : "Internal only",
      );
      button.type = "button";
      button.disabled = agent.state !== "deployed" || !agent.customerFacing;
      button.addEventListener("click", () => {
        byId("playAgentSelect").value = agent.id;
        resetSession();
        byId("tabbtn-playground").click();
      });
      foot.append(type, button);
      card.append(head, facts, foot);
      grid.append(card);
    });
    renderAgentOptions();
  }

  function renderAgentOptions() {
    const select = byId("playAgentSelect");
    if (!select) return;
    const previous = select.value;
    const deployed = state.agents.filter(
      (agent) => agent.state === "deployed" && agent.customerFacing,
    );
    select.replaceChildren();
    if (!deployed.length) {
      const option = el("option", "", "No deployed runtimes");
      option.value = "";
      select.append(option);
    } else {
      deployed.forEach((agent) => {
        const option = el("option", "", `${agent.name} · ${agent.protocol}`);
        option.value = agent.id;
        select.append(option);
      });
      if (deployed.some((agent) => agent.id === previous)) select.value = previous;
    }
    updateControls();
  }

  function updateControls() {
    const selected = byId("playAgentSelect")?.value;
    const disabled = state.busy || !state.interactive || !selected;
    byId("sendPrompt").disabled = disabled;
    byId("playPrompt").disabled = state.busy || !state.interactive || !selected;
    byId("playAgentSelect").disabled =
      state.busy ||
      !state.agents.some(
        (agent) => agent.state === "deployed" && agent.customerFacing,
      );
    byId("newSession").disabled = state.busy;
    byId("consoleRequired").hidden = state.interactive;
  }

  function appendMessage(role, text, meta) {
    const message = el("div", `message message--${role}`);
    const avatar = el("div", "message-avatar", role === "user" ? "You" : "AC");
    avatar.setAttribute("aria-hidden", "true");
    const content = el("div");
    content.append(el("div", "message-meta", meta));
    content.append(el("div", "message-body", text));
    message.append(avatar, content);
    const messages = byId("chatMessages");
    messages.append(message);
    messages.scrollTop = messages.scrollHeight;
  }

  function welcomeMessage() {
    const messages = byId("chatMessages");
    messages.replaceChildren();
    appendMessage(
      "assistant",
      "Choose a deployed agent and send a synthetic test prompt. The trace rail shows transport, latency, and tool events without exposing credentials.",
      "AgentCore",
    );
  }

  function setTraceState(label, tone = "") {
    const badge = byId("traceState");
    badge.textContent = label;
    badge.className = `badge ${tone}`.trim();
  }

  function setTraceEvents(events) {
    const list = byId("traceEvents");
    list.replaceChildren();
    events.forEach((event) => {
      const item = el("li", event.kind ? `trace-${event.kind}` : "");
      item.append(el("span", "trace-mark"), el("span", "", event.label));
      list.append(item);
    });
  }

  function resetTrace() {
    byId("traceSession").textContent = state.sessionId;
    byId("traceTarget").textContent = byId("playAgentSelect").value || "—";
    byId("traceProtocol").textContent = "—";
    byId("traceLatency").textContent = "—";
    setTraceState("Ready");
    setTraceEvents([{ label: "Waiting for an invocation" }]);
  }

  function resetSession() {
    state.sessionId = sessionId();
    welcomeMessage();
    resetTrace();
  }

  async function checkHealth() {
    try {
      const response = await fetch("/api/health", { cache: "no-store" });
      const data = await response.json();
      state.csrfToken = typeof data.csrfToken === "string" ? data.csrfToken : "";
      state.interactive =
        response.ok &&
        data.interactive === true &&
        data.localOnly === true &&
        state.csrfToken.length >= 32;
    } catch {
      state.interactive = false;
      state.csrfToken = "";
    }
    state.healthChecked = true;
    updateControls();
  }

  async function submitPrompt(event) {
    event.preventDefault();
    const prompt = byId("playPrompt").value.trim();
    const component = byId("playAgentSelect").value;
    if (!prompt || !component || state.busy || !state.interactive) return;

    state.busy = true;
    updateControls();
    appendMessage("user", prompt, "You");
    byId("playPrompt").value = "";
    byId("promptCount").textContent = "0";
    byId("traceSession").textContent = state.sessionId;
    byId("traceTarget").textContent = component;
    byId("traceProtocol").textContent = "Connecting…";
    byId("traceLatency").textContent = "—";
    setTraceState("Running", "info");
    setTraceEvents([{ kind: "info", label: "Invocation started" }]);

    try {
      const response = await fetch("/api/invoke", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-EBA-CSRF": state.csrfToken,
        },
        body: JSON.stringify({
          component,
          prompt,
          sessionId: state.sessionId,
        }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "Agent invocation failed.");
      appendMessage("assistant", result.output || "The runtime returned no display text.", component);
      byId("traceProtocol").textContent = result.protocol || "—";
      byId("traceLatency").textContent = `${result.durationMs} ms`;
      setTraceState("Complete", "ok");
      const events = [
        { kind: "ok", label: "Runtime response received" },
        ...(Array.isArray(result.trace) ? result.trace : []),
      ];
      setTraceEvents(events);
    } catch (error) {
      const message = error instanceof Error ? error.message : "Agent invocation failed.";
      appendMessage("assistant", message, "Invocation error");
      byId("traceProtocol").textContent = "—";
      setTraceState("Failed", "err");
      setTraceEvents([{ kind: "error", label: message }]);
    } finally {
      state.busy = false;
      updateControls();
      byId("playPrompt").focus();
    }
  }

  function update(data) {
    state.statusAvailable = Boolean(data);
    state.agents = agentsFromStatus(data);
    renderAgents();
    if (!state.sessionId) resetSession();
  }

  function activate(tab) {
    if (tab === "playground" && !state.healthChecked) checkHealth();
  }

  byId("playForm").addEventListener("submit", submitPrompt);
  byId("playPrompt").addEventListener("input", (event) => {
    byId("promptCount").textContent = String(event.target.value.length);
  });
  byId("playPrompt").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      byId("playForm").requestSubmit();
    }
  });
  byId("newSession").addEventListener("click", resetSession);
  byId("playAgentSelect").addEventListener("change", resetSession);
  state.sessionId = sessionId();
  resetTrace();
  checkHealth();

  window.EbaConsole = { activate, update };
})();
