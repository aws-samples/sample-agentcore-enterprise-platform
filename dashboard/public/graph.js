/* AgentCore Accelerator — X-Ray service map.
 *
 * This is the Agent Studio demo's X-Ray interaction model adapted to the
 * accelerator's read-only status.json contract: service bands, compact nodes,
 * labelled relationships, draggable headers, and a right-side inspection
 * sheet. It deliberately does not expose control-plane mutations.
 */
(function () {
  'use strict';

  var NS = 'http://www.w3.org/2000/svg';
  var STACK_SUFFIXES = [
    'networking',
    'security',
    'auth',
    'identity',
    'gateway',
    'memory',
    'runtime-orchestrator',
    'runtime-code-agent',
    'runtime-research-agent',
    'observability'
  ];

  var SERVICES = {
    client: {
      title: 'Customer / EBA client',
      icon: 'user',
      blurb: 'The customer-facing application or approved EBA client that starts an authenticated agent session.'
    },
    idp: {
      title: 'Enterprise identity provider',
      icon: 'fingerprint',
      blurb: 'The enterprise identity provider authenticates users before Cognito issues the token accepted by the platform.'
    },
    auth: {
      title: 'Amazon Cognito',
      icon: 'shield',
      blurb: 'The platform authentication boundary. It federates the configured identity provider and issues scoped OAuth tokens.'
    },
    identity: {
      title: 'AgentCore Identity',
      icon: 'key',
      blurb: 'Credential providers and the token vault keep downstream credentials out of agent prompts and application code.',
      docs: 'https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/identity.html'
    },
    runtime: {
      title: 'AgentCore Runtime',
      icon: 'cpu',
      blurb: 'Serverless, session-isolated compute for the customer-facing orchestrator and its specialist agents.',
      docs: 'https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime.html'
    },
    gateway: {
      title: 'AgentCore Gateway',
      icon: 'server',
      blurb: 'The governed MCP entry point for tools, with inbound authorization and controlled outbound credential use.',
      docs: 'https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway.html'
    },
    target: {
      title: 'Gateway targets',
      icon: 'layers',
      blurb: 'Approved Lambda, API, or MCP backends exposed as tools through the Gateway. Target names are intentionally not published by this dashboard.'
    },
    memory: {
      title: 'AgentCore Memory',
      icon: 'database',
      blurb: 'Short-term session context and optional long-term strategies shared through the platform memory boundary.',
      docs: 'https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory.html'
    },
    observability: {
      title: 'Observability',
      icon: 'activity',
      blurb: 'CloudWatch and X-Ray receive the platform telemetry used to inspect sessions, errors, latency, and tool activity.',
      docs: 'https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability.html'
    },
    security: {
      title: 'Security and audit',
      icon: 'lock',
      blurb: 'Optional customer-managed KMS and CloudTrail controls for retained production evidence and audit boundaries.'
    },
    networking: {
      title: 'Private networking',
      icon: 'network',
      blurb: 'Optional VPC, private subnets, security groups, and endpoints for runtimes that require private dependencies.'
    }
  };

  var ICONS = {
    user: ['circle:12,7,3.2', 'path:M5.5 20c.5-4 2.7-6 6.5-6s6 2 6.5 6'],
    fingerprint: ['path:M8.5 11.5c0-2 1.4-3.5 3.5-3.5s3.5 1.5 3.5 3.5c0 4.7-1.4 7.5-3.5 9', 'path:M5 11.5C5 7.3 7.8 4 12 4s7 3.3 7 7.5c0 4-.8 6.9-2.3 9', 'path:M9 15.5c.2 2-.2 3.8-1.2 5'],
    shield: ['path:M12 3l7 3v5c0 4.7-2.8 8-7 10-4.2-2-7-5.3-7-10V6z', 'path:M9 12l2 2 4-4'],
    key: ['circle:8,15,4', 'path:M11 12l8-8M16 7l2 2M14 9l2 2'],
    cpu: ['rect:7,7,10,10,2', 'path:M9 1v4M15 1v4M9 19v4M15 19v4M1 9h4M1 15h4M19 9h4M19 15h4', 'path:M10 10h4v4h-4z'],
    server: ['rect:4,3,16,7,2', 'rect:4,14,16,7,2', 'path:M8 7h.01M8 18h.01M12 7h6M12 18h6'],
    layers: ['path:M12 2l9 5-9 5-9-5z', 'path:M3 12l9 5 9-5', 'path:M3 17l9 5 9-5'],
    database: ['ellipse:12,5,8,3', 'path:M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5', 'path:M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6'],
    activity: ['path:M3 12h4l2.5-7 5 14 2.5-7h4'],
    lock: ['rect:5,10,14,11,2', 'path:M8 10V7a4 4 0 0 1 8 0v3M12 14v3'],
    network: ['rect:9,2,6,5,1', 'rect:2,17,6,5,1', 'rect:16,17,6,5,1', 'path:M12 7v5M5 17v-3h14v3']
  };

  var root = null;
  var canvas = null;
  var inner = null;
  var bandLayer = null;
  var nodeLayer = null;
  var edgeLayer = null;
  var labelLayer = null;
  var sheetBackdrop = null;
  var sheetTitle = null;
  var sheetSubtitle = null;
  var sheetBody = null;
  var sheetFooter = null;
  var model = null;
  var pendingStatus = null;
  var positions = {};
  var selectedId = null;
  var returnFocus = null;
  var listeners = [];
  var resizeObserver = null;
  var drag = null;

  function on(target, type, handler, options) {
    target.addEventListener(type, handler, options || false);
    listeners.push([target, type, handler, options || false]);
  }

  function el(tag, className, parent, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    if (parent) parent.appendChild(node);
    return node;
  }

  function svgEl(tag, className, parent) {
    var node = document.createElementNS(NS, tag);
    if (className) node.setAttribute('class', className);
    if (parent) parent.appendChild(node);
    return node;
  }

  function icon(name, parent) {
    var svg = svgEl('svg', 'xr-icon', parent);
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '1.8');
    svg.setAttribute('stroke-linecap', 'round');
    svg.setAttribute('stroke-linejoin', 'round');
    (ICONS[name] || ICONS.layers).forEach(function (instruction) {
      var split = instruction.indexOf(':');
      var kind = instruction.slice(0, split);
      var value = instruction.slice(split + 1);
      var shape = svgEl(kind, '', svg);
      if (kind === 'path') shape.setAttribute('d', value);
      if (kind === 'circle') {
        var c = value.split(',');
        shape.setAttribute('cx', c[0]);
        shape.setAttribute('cy', c[1]);
        shape.setAttribute('r', c[2]);
      }
      if (kind === 'ellipse') {
        var e = value.split(',');
        shape.setAttribute('cx', e[0]);
        shape.setAttribute('cy', e[1]);
        shape.setAttribute('rx', e[2]);
        shape.setAttribute('ry', e[3]);
      }
      if (kind === 'rect') {
        var r = value.split(',');
        shape.setAttribute('x', r[0]);
        shape.setAttribute('y', r[1]);
        shape.setAttribute('width', r[2]);
        shape.setAttribute('height', r[3]);
        if (r[4]) {
          shape.setAttribute('rx', r[4]);
          shape.setAttribute('ry', r[4]);
        }
      }
    });
    return svg;
  }

  function suffixOf(key, project, environment) {
    var prefix = String(project || '') + '-' + String(environment || '') + '-';
    return key.indexOf(prefix) === 0 ? key.slice(prefix.length) : key;
  }

  function stackRecords(status) {
    var stacks = status && typeof status.stacks === 'object' && !Array.isArray(status.stacks)
      ? status.stacks : {};
    var records = {};
    Object.keys(stacks).forEach(function (key) {
      var record = stacks[key];
      if (!record || typeof record !== 'object' || Array.isArray(record)) return;
      var suffix = suffixOf(key, status.project, status.environment);
      if (STACK_SUFFIXES.indexOf(suffix) >= 0) records[suffix] = record;
    });
    return records;
  }

  function stateOf(record) {
    return record && typeof record.state === 'string' && record.state
      ? record.state : 'not-deployed';
  }

  function visualState(sourceState) {
    if (sourceState === 'deployed') return 'live';
    if (sourceState === 'in-progress') return 'planned';
    if (sourceState === 'failed') return 'failed';
    if (sourceState === 'not-applicable') return 'avail';
    if (sourceState === 'unobserved') return 'unobserved';
    return 'not-deployed';
  }

  function stateLabel(sourceState) {
    return {
      deployed: 'live',
      'in-progress': 'deploying',
      failed: 'failed',
      'not-deployed': 'not deployed',
      'not-applicable': 'not enabled',
      unobserved: 'not observed'
    }[sourceState] || 'unknown';
  }

  function output(record, name) {
    var outputs = record && typeof record.outputs === 'object' && !Array.isArray(record.outputs)
      ? record.outputs : {};
    var value = outputs[name];
    return value === undefined || value === null || value === '' ? '—' : String(value);
  }

  function short(value, limit) {
    var text = String(value || '—');
    return text.length > limit ? text.slice(0, Math.max(1, limit - 1)) + '…' : text;
  }

  function idpLabel(value) {
    return {
      entra_id: 'Microsoft Entra ID',
      okta: 'Okta',
      cognito: 'Cognito local users',
      none: 'No external IdP'
    }[String(value || '').toLowerCase()] || String(value || 'Enterprise IdP');
  }

  function resourceCount(record, type) {
    var resources = record && Array.isArray(record.resources) ? record.resources : [];
    return resources.filter(function (resource) {
      return resource && resource.type === type;
    }).length;
  }

  function makeNode(id, service, title, record, x, y, lines, details, options) {
    var sourceState = options && options.sourceState
      ? options.sourceState : stateOf(record);
    return {
      id: id,
      service: service,
      title: title || SERVICES[service].title,
      sourceState: sourceState,
      visualState: visualState(sourceState),
      x: x,
      y: y,
      width: options && options.width ? options.width : 240,
      lines: lines || [],
      details: details || []
    };
  }

  function buildModel(status) {
    var deployment = status && typeof status.deployment === 'object' && !Array.isArray(status.deployment)
      ? status.deployment : {};
    var records = stackRecords(status || {});
    var auth = records.auth || {};
    var identity = records.identity || {};
    var gateway = records.gateway || {};
    var orchestrator = records['runtime-orchestrator'] || {};
    var research = records['runtime-research-agent'] || {};
    var code = records['runtime-code-agent'] || {};
    var memory = records.memory || {};
    var observability = records.observability || {};
    var security = records.security || {};
    var networking = records.networking || {};
    var authState = stateOf(auth);
    var idpType = output(auth, 'IdPType');
    var idpMode = output(auth, 'IdPMode');
    var targetCount = resourceCount(gateway, 'AWS::BedrockAgentCore::GatewayTarget');
    var nodes = [];

    nodes.push(makeNode(
      'client', 'client', 'Customer / EBA client', auth, 40, 70,
      [['entry', 'approved client'], ['scope', 'customer test']],
      [['Relationship', 'Starts an authenticated runtime session'], ['EBA use', 'Synthetic or approved test data only']],
      { sourceState: authState }
    ));
    nodes.push(makeNode(
      'idp', 'idp', idpLabel(idpType), auth, 330, 70,
      [['provider', idpLabel(idpType)], ['mode', idpMode]],
      [['Provider', idpLabel(idpType)], ['Federation mode', idpMode], ['Source', 'Reviewed auth stack output']],
      { sourceState: idpType === '—' || idpType === 'none' ? 'not-applicable' : authState }
    ));
    nodes.push(makeNode(
      'auth', 'auth', 'Amazon Cognito', auth, 620, 70,
      [['issuer', short(output(auth, 'IssuerUrl'), 24)], ['flow', 'OAuth 2.0 / JWT']],
      [['State', stateLabel(authState)], ['Issuer', output(auth, 'IssuerUrl')], ['User pool', output(auth, 'UserPoolId')]]
    ));
    nodes.push(makeNode(
      'identity', 'identity', 'AgentCore Identity', identity, 910, 70,
      [['provider', short(output(identity, 'GatewayCredentialProviderName'), 20)], ['secrets', 'token vault']],
      [['State', stateLabel(stateOf(identity))], ['Gateway credential provider', output(identity, 'GatewayCredentialProviderName')]]
    ));

    nodes.push(makeNode(
      'orchestrator', 'runtime', 'Orchestrator', orchestrator, 60, 280,
      [['role', 'customer-facing'], ['protocol', protocolFor(status)]],
      [['State', stateLabel(stateOf(orchestrator))], ['Runtime ID', output(orchestrator, 'RuntimeId')], ['Runtime ARN', output(orchestrator, 'RuntimeArn')]],
      { width: 280 }
    ));
    nodes.push(makeNode(
      'research', 'runtime', 'Research agent', research, 360, 420,
      [['role', 'A2A specialist'], ['access', 'internal only']],
      [['State', stateLabel(stateOf(research))], ['Runtime ID', output(research, 'RuntimeId')], ['Invocation', 'Only through the orchestrator']]
    ));
    nodes.push(makeNode(
      'code', 'runtime', 'Code agent', code, 360, 560,
      [['role', 'A2A specialist'], ['access', 'internal only']],
      [['State', stateLabel(stateOf(code))], ['Runtime ID', output(code, 'RuntimeId')], ['Invocation', 'Only through the orchestrator']]
    ));

    nodes.push(makeNode(
      'gateway', 'gateway', 'AgentCore Gateway', gateway, 720, 280,
      [['protocol', 'MCP'], ['targets', String(targetCount)]],
      [['State', stateLabel(stateOf(gateway))], ['Gateway ID', output(gateway, 'GatewayId')], ['Gateway URL', output(gateway, 'GatewayUrl')]]
    ));
    nodes.push(makeNode(
      'targets', 'target', 'Gateway targets', gateway, 1010, 400,
      [['resources', String(targetCount)], ['details', 'intentionally hidden']],
      [['Target count', String(targetCount)], ['Published detail', 'Names and backend configuration are not exposed in status.json']],
      { sourceState: targetCount > 0 ? stateOf(gateway) : 'not-deployed' }
    ));

    nodes.push(makeNode(
      'memory', 'memory', 'AgentCore Memory', memory, 40, 820,
      [['memory', short(output(memory, 'MemoryId'), 18)], ['scope', 'session + long-term']],
      [['State', stateLabel(stateOf(memory))], ['Memory ID', output(memory, 'MemoryId')], ['Memory ARN', output(memory, 'MemoryArn')]]
    ));
    nodes.push(makeNode(
      'observability', 'observability', 'Observability', observability, 295, 820,
      [['telemetry', 'logs + traces'], ['resources', output(observability, 'MonitoredResources')]],
      [['State', stateLabel(stateOf(observability))], ['Monitored resources', output(observability, 'MonitoredResources')]]
    ));
    nodes.push(makeNode(
      'security', 'security', 'Security and audit', security, 550, 820,
      [['controls', 'KMS + CloudTrail'], ['profile', stateLabel(stateOf(security))]],
      [['State', stateLabel(stateOf(security))], ['Enable with', 'security.enabled: true'], ['KMS key', output(security, 'KmsKeyArn')]]
    ));
    nodes.push(makeNode(
      'networking', 'networking', 'Private networking', networking, 805, 820,
      [['controls', 'VPC + endpoints'], ['profile', stateLabel(stateOf(networking))]],
      [['State', stateLabel(stateOf(networking))], ['Enable with', 'networking.enabled: true'], ['VPC', output(networking, 'VpcId')]]
    ));

    var byId = {};
    nodes.forEach(function (node) { byId[node.id] = node; });
    function edge(from, to, label, className) {
      var a = byId[from];
      var b = byId[to];
      var stateClass = '';
      if (a.visualState === 'avail' || b.visualState === 'avail' ||
          a.visualState === 'not-deployed' || b.visualState === 'not-deployed') stateClass = 'avail';
      if (a.visualState === 'unobserved' || b.visualState === 'unobserved') stateClass = 'unobserved';
      if (a.visualState === 'failed' || b.visualState === 'failed') stateClass = 'failed';
      return { from: from, to: to, label: label, className: [className || '', stateClass].filter(Boolean).join(' ') };
    }
    var edges = [
      edge('client', 'idp', 'sign-in'),
      edge('idp', 'auth', 'OIDC / SAML'),
      edge('auth', 'orchestrator', 'Bearer JWT'),
      edge('identity', 'gateway', 'credential provider'),
      edge('orchestrator', 'gateway', 'MCP tools'),
      edge('gateway', 'targets', 'governed calls'),
      edge('orchestrator', 'research', 'A2A · IAM', 'a2a'),
      edge('orchestrator', 'code', 'A2A · IAM', 'a2a'),
      edge('orchestrator', 'memory', 'session context'),
      edge('orchestrator', 'observability', 'OTel spans'),
      edge('orchestrator', 'networking', 'VPC attachment')
    ];

    var role = deployment.role ? ' · ' + deployment.role + ' account' : '';
    return {
      nodes: nodes,
      edges: edges,
      bands: [
        { label: 'Identity & access', x: 20, y: 40, width: 1260, height: 180 },
        { label: 'Agent runtimes', x: 20, y: 240, width: 620, height: 520 },
        { label: 'Tools & integrations', x: 680, y: 240, width: 600, height: 520 },
        { label: 'Platform services', x: 20, y: 790, width: 1260, height: 190 }
      ],
      width: 1320,
      height: 1010,
      context: String(deployment.strategy || 'centralized') + role
    };
  }

  function protocolFor(status) {
    if (status && status.migration_runtime) return 'HTTP · migration';
    return String(status && status.agent_pattern || '').indexOf('agui-') === 0
      ? 'AG-UI · OAuth' : 'HTTP · OAuth';
  }

  function badgeClass(sourceState) {
    return {
      deployed: 'ok',
      'in-progress': 'warn',
      failed: 'err',
      'not-deployed': 'secondary',
      'not-applicable': 'secondary',
      unobserved: 'secondary'
    }[sourceState] || 'secondary';
  }

  function createToolbar(host) {
    var toolbar = el('div', 'xr-bar', host);
    var legend = el('div', 'xr-legend', toolbar);
    [
      ['ok', 'live'],
      ['warn', 'deploying'],
      ['err', 'failed'],
      ['secondary', 'available, not enabled']
    ].forEach(function (item) {
      var badge = el('span', 'xr-badge ' + item[0], legend);
      el('span', 'xr-dot', badge);
      badge.appendChild(document.createTextNode(item[1]));
    });
    el('span', 'xr-grow', toolbar);
    el('span', 'xr-hint', toolbar, 'Click a node to inspect it. Drag headers to rearrange.');
    var reset = el('button', 'xr-reset', toolbar, 'Reset layout');
    reset.type = 'button';
    on(reset, 'click', function () {
      positions = {};
      selectedId = null;
      closeSheet();
      render();
    });
  }

  function createSheet(host) {
    sheetBackdrop = el('div', 'xr-sheet-bg', host);
    sheetBackdrop.hidden = true;
    var sheet = el('aside', 'xr-sheet', sheetBackdrop);
    sheet.setAttribute('role', 'dialog');
    sheet.setAttribute('aria-modal', 'true');
    sheet.setAttribute('aria-labelledby', 'xrSheetTitle');
    var header = el('div', 'xr-sheet-head', sheet);
    var heading = el('div', 'xr-sheet-heading', header);
    sheetTitle = el('h2', '', heading);
    sheetTitle.id = 'xrSheetTitle';
    sheetSubtitle = el('p', '', heading);
    var close = el('button', 'xr-sheet-close', header, '×');
    close.type = 'button';
    close.setAttribute('aria-label', 'Close X-Ray details');
    sheetBody = el('div', 'xr-sheet-body', sheet);
    sheetFooter = el('div', 'xr-sheet-foot', sheet);
    on(close, 'click', closeSheet);
    on(sheetBackdrop, 'click', function (event) {
      if (event.target === sheetBackdrop) closeSheet();
    });
  }

  function mount(host) {
    if (!host) return;
    if (root === host && root.childElementCount) return;
    destroy();
    root = host;
    root.replaceChildren();
    var shell = el('div', 'xr-shell', root);
    createToolbar(shell);
    canvas = el('div', 'xr-canvas', shell);
    canvas.id = 'xrCanvas';
    canvas.setAttribute('aria-label', 'AgentCore X-Ray service map');
    inner = el('div', 'xr-inner', canvas);
    edgeLayer = svgEl('svg', 'xr-edges', inner);
    edgeLayer.setAttribute('aria-hidden', 'true');
    bandLayer = el('div', 'xr-bands', inner);
    nodeLayer = el('div', 'xr-nodes', inner);
    labelLayer = svgEl('svg', 'xr-labels', inner);
    labelLayer.setAttribute('aria-hidden', 'true');
    createSheet(shell);
    on(inner, 'pointerdown', pointerDown);
    on(inner, 'pointermove', pointerMove);
    on(inner, 'pointerup', pointerUp);
    on(inner, 'pointercancel', pointerUp);
    on(document, 'keydown', function (event) {
      if (event.key === 'Escape' && sheetBackdrop && !sheetBackdrop.hidden) closeSheet();
    });
    if (typeof ResizeObserver === 'function') {
      resizeObserver = new ResizeObserver(drawEdges);
      resizeObserver.observe(root);
    }
    if (pendingStatus) render();
  }

  function renderBands() {
    bandLayer.replaceChildren();
    model.bands.forEach(function (band) {
      var box = el('div', 'xr-band', bandLayer);
      box.style.left = band.x + 'px';
      box.style.top = band.y + 'px';
      box.style.width = band.width + 'px';
      box.style.height = band.height + 'px';
      el('span', 'xr-band-label', box, band.label);
    });
  }

  function renderNodes() {
    nodeLayer.replaceChildren();
    model.nodes.forEach(function (node) {
      var position = positions[node.id] || { x: node.x, y: node.y };
      positions[node.id] = position;
      var card = el('div', 'xr-node xr-' + node.visualState, nodeLayer);
      card.dataset.node = node.id;
      card.style.left = position.x + 'px';
      card.style.top = position.y + 'px';
      card.style.width = node.width + 'px';
      card.tabIndex = 0;
      card.setAttribute('role', 'button');
      card.setAttribute('aria-label', node.title + ', ' + stateLabel(node.sourceState) + '. Inspect service.');
      if (selectedId === node.id) card.classList.add('selected');
      el('span', 'xr-port in', card);
      var head = el('div', 'xr-node-head', card);
      icon(SERVICES[node.service].icon, head);
      el('span', 'xr-node-title', head, node.title);
      el('span', 'xr-badge ' + badgeClass(node.sourceState), head, stateLabel(node.sourceState));
      var body = el('div', 'xr-node-body', card);
      body.title = 'Inspect';
      var grid = el('div', 'xr-kvs', body);
      node.lines.forEach(function (line) {
        el('div', 'xr-key', grid, line[0]);
        el('div', 'xr-value', grid, line[1]);
      });
      el('span', 'xr-port out', card);
      body.addEventListener('click', function () { openSheet(node.id, card); });
      card.addEventListener('keydown', function (event) {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          openSheet(node.id, card);
        }
      });
    });
  }

  function drawEdges() {
    if (!model || !nodeLayer || !edgeLayer || !labelLayer) return;
    edgeLayer.replaceChildren();
    labelLayer.replaceChildren();
    model.edges.forEach(function (edge) {
      var a = nodeLayer.querySelector('[data-node="' + CSS.escape(edge.from) + '"]');
      var b = nodeLayer.querySelector('[data-node="' + CSS.escape(edge.to) + '"]');
      if (!a || !b) return;
      var ar = { x: a.offsetLeft, y: a.offsetTop, width: a.offsetWidth, height: a.offsetHeight };
      var br = { x: b.offsetLeft, y: b.offsetTop, width: b.offsetWidth, height: b.offsetHeight };
      var pathData;
      var labelX;
      var labelY;
      if (br.y > ar.y + ar.height + 40 &&
          Math.abs((br.x + br.width / 2) - (ar.x + ar.width / 2)) < Math.max(ar.width, 400)) {
        var vx1 = ar.x + ar.width / 2;
        var vy1 = ar.y + ar.height;
        var vx2 = br.x + br.width / 2;
        var vy2 = br.y;
        var vc = Math.max(30, (vy2 - vy1) / 2);
        pathData = 'M' + vx1 + ',' + vy1 + ' C' + vx1 + ',' + (vy1 + vc) + ' ' +
          vx2 + ',' + (vy2 - vc) + ' ' + vx2 + ',' + vy2;
        labelX = (vx1 + vx2) / 2;
        labelY = (vy1 + vy2) / 2 - 4;
      } else {
        var leftToRight = br.x >= ar.x + ar.width / 2;
        var hx1 = leftToRight ? ar.x + ar.width : ar.x;
        var hy1 = ar.y + ar.height / 2;
        var hx2 = leftToRight ? br.x : br.x + br.width;
        var hy2 = br.y + br.height / 2;
        var hc = Math.max(40, Math.abs(hx2 - hx1) / 2);
        pathData = 'M' + hx1 + ',' + hy1 + ' C' +
          (hx1 + (leftToRight ? hc : -hc)) + ',' + hy1 + ' ' +
          (hx2 - (leftToRight ? hc : -hc)) + ',' + hy2 + ' ' + hx2 + ',' + hy2;
        labelX = (hx1 + hx2) / 2;
        labelY = (hy1 + hy2) / 2 - 6;
      }
      var path = svgEl('path', 'xr-edge ' + edge.className, edgeLayer);
      path.setAttribute('d', pathData);
      if (edge.label) {
        var label = svgEl('text', 'xr-edge-label ' + edge.className, labelLayer);
        label.setAttribute('x', labelX);
        label.setAttribute('y', labelY);
        label.textContent = edge.label;
      }
    });
  }

  function render() {
    if (!root || !pendingStatus) return;
    model = buildModel(pendingStatus);
    inner.style.width = model.width + 'px';
    inner.style.height = model.height + 'px';
    edgeLayer.setAttribute('width', model.width);
    edgeLayer.setAttribute('height', model.height);
    labelLayer.setAttribute('width', model.width);
    labelLayer.setAttribute('height', model.height);
    renderBands();
    renderNodes();
    drawEdges();
    if (selectedId && sheetBackdrop && !sheetBackdrop.hidden) fillSheet(selectedId);
  }

  function fillSheet(id) {
    var node = model && model.nodes.find(function (item) { return item.id === id; });
    if (!node) {
      closeSheet();
      return;
    }
    var service = SERVICES[node.service];
    sheetTitle.textContent = node.title;
    sheetSubtitle.textContent = node.title === service.title
      ? stateLabel(node.sourceState) : service.title;
    sheetBody.replaceChildren();
    var stateRow = el('p', 'xr-sheet-state', sheetBody);
    el('span', 'xr-badge ' + badgeClass(node.sourceState), stateRow, stateLabel(node.sourceState));
    el('p', 'xr-sheet-blurb', sheetBody, service.blurb);
    el('h3', '', sheetBody, node.sourceState === 'not-applicable' ? 'What it adds' : 'Live configuration');
    var details = el('dl', 'xr-detail-list', sheetBody);
    node.details.forEach(function (detail) {
      el('dt', '', details, detail[0]);
      el('dd', '', details, detail[1]);
    });
    sheetFooter.replaceChildren();
    if (service.docs) {
      var docs = el('a', 'xr-sheet-action secondary', sheetFooter, 'AWS documentation');
      docs.href = service.docs;
      docs.target = '_blank';
      docs.rel = 'noopener noreferrer';
    }
    var close = el('button', 'xr-sheet-action', sheetFooter, 'Close');
    close.type = 'button';
    close.addEventListener('click', closeSheet);
  }

  function openSheet(id, trigger) {
    selectedId = id;
    returnFocus = trigger || null;
    nodeLayer.querySelectorAll('.xr-node').forEach(function (node) {
      node.classList.toggle('selected', node.dataset.node === id);
    });
    fillSheet(id);
    sheetBackdrop.hidden = false;
    var close = sheetBackdrop.querySelector('.xr-sheet-close');
    if (close) close.focus();
  }

  function closeSheet() {
    if (!sheetBackdrop || sheetBackdrop.hidden) return;
    sheetBackdrop.hidden = true;
    selectedId = null;
    if (nodeLayer) {
      nodeLayer.querySelectorAll('.xr-node').forEach(function (node) {
        node.classList.remove('selected');
      });
    }
    if (returnFocus && document.contains(returnFocus)) returnFocus.focus();
    returnFocus = null;
  }

  function pointerDown(event) {
    var head = event.target.closest('.xr-node-head');
    if (!head || !inner.contains(head)) return;
    var card = head.closest('.xr-node');
    var position = positions[card.dataset.node];
    if (!position) return;
    drag = {
      card: card,
      id: card.dataset.node,
      dx: event.clientX - position.x,
      dy: event.clientY - position.y,
      pointerId: event.pointerId
    };
    card.classList.add('dragging');
    head.setPointerCapture(event.pointerId);
    event.preventDefault();
  }

  function pointerMove(event) {
    if (!drag || event.pointerId !== drag.pointerId) return;
    var position = positions[drag.id];
    var width = drag.card.offsetWidth;
    var height = drag.card.offsetHeight;
    position.x = Math.max(0, Math.min(model.width - width, event.clientX - drag.dx));
    position.y = Math.max(0, Math.min(model.height - height, event.clientY - drag.dy));
    drag.card.style.left = position.x + 'px';
    drag.card.style.top = position.y + 'px';
    drawEdges();
  }

  function pointerUp(event) {
    if (!drag || event.pointerId !== drag.pointerId) return;
    drag.card.classList.remove('dragging');
    drag = null;
  }

  function update(status) {
    if (!status || typeof status !== 'object' || Array.isArray(status)) return;
    pendingStatus = status;
    if (root) render();
  }

  function destroy() {
    listeners.forEach(function (entry) {
      entry[0].removeEventListener(entry[1], entry[2], entry[3]);
    });
    listeners = [];
    if (resizeObserver) resizeObserver.disconnect();
    resizeObserver = null;
    drag = null;
    selectedId = null;
    returnFocus = null;
    if (root) root.replaceChildren();
    root = null;
    canvas = null;
    inner = null;
    bandLayer = null;
    nodeLayer = null;
    edgeLayer = null;
    labelLayer = null;
    sheetBackdrop = null;
    sheetTitle = null;
    sheetSubtitle = null;
    sheetBody = null;
    sheetFooter = null;
    model = null;
  }

  window.ArchGraph = {
    mount: mount,
    update: update,
    destroy: destroy
  };
})();
