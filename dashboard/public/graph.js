/* AgentCore Accelerator — live architecture view (Architecture tab), v4.
 *
 * Zero dependencies on purpose: the dashboard is a single self-contained folder
 * that has to work over `python3 -m http.server`, so no React, no xyflow, no
 * CDN, no build step. This is plain DOM + inline SVG.
 *
 * v1 was a force-directed hairball. v2 was the right diagram drawn the wrong
 * way: one <canvas>, so every label went through measureText and got shrunk or
 * ellipsed, nothing could be selected or copied, and the whole picture was
 * invisible to the accessibility tree. v3 fixed the renderer but dressed it as
 * a dark engineering diagram. v4 changes ONLY the presentation layer — data
 * model, layout maths, semantics and API are v3's:
 *
 * A. LIGHT FLOW-MAP LANGUAGE, not a wiring schematic. The pane is a bright
 *    design canvas inside the dark shell (its own --rf-* variables in
 *    graph.css, deliberately NOT the page's dark --bg), and a card is a small
 *    quiet white tile: coloured icon square on the left, at most two ellipsised
 *    lines of small type. State lives in the icon tile's fill, so the colour is
 *    one glance and the text stays black-on-white legible.
 * B. CURVED EDGES. Cubic beziers leaving the source's right edge horizontally
 *    and entering the target's left edge horizontally, so a fan-out from one
 *    card and a merge into a shared target read as flow rather than as bus
 *    wiring. See bezier() for the control-point rule.
 * C. LESS CHROME. The minimap is gone (a 10-card diagram that fits the pane
 *    never needed one) and the decorative handle dots with it: neither carried
 *    information. Anything that implies an action we cannot perform — xyflow
 *    handles, the reference's mid-edge "+" buttons — stays out, because this
 *    view is read-only and a dead affordance is worse than none.
 *
 * Inherited from v3, unchanged:
 *
 * 1. DOM OVER CANVAS. A stack name is text; making it real text means crisp
 *    subpixel rendering at any zoom, selectable/copyable output in the details
 *    panel, native title tooltips, tabbing, and CSS doing the styling. The
 *    whole diagram is ~12 cards — DOM is nowhere near a bottleneck here.
 * 2. DETERMINISTIC layout, no physics. Columns are layers left→right, cards
 *    stack inside a column. Same status.json ⇒ same picture, so a narrated demo
 *    can say "the gateway is here" and still be right tomorrow.
 * 3. `state` is READ, never recomputed. dashboard/monitor.py owns the one and
 *    only CloudFormation-status classifier; a second one in the UI was the bug
 *    we removed. Missing state degrades to "not-deployed", never inferred from
 *    the raw `status` string here.
 * 4. OBSERVABILITY IS A BADGE, NOT EDGES. Five stacks ship logs+traces to the
 *    observability stack; as edges that is a fan-in through the middle of the
 *    picture, v1's worst crossing source. TOPOLOGY stays complete (it mirrors
 *    app.py) and those edges are filtered at draw time — see buildEdges().
 * 5. HONESTY about what a dashboard can see: exactly ONE account
 *    (deployment.polled_account / role). The other side of a federation is
 *    unknown from here, so it is drawn `unobserved` (dashed, dim, no state
 *    colour) and never counted as deployed or failed.
 * 6. NO ANIMATION LOOP AT ALL. Every animation is a CSS @keyframes or a CSS
 *    transition, so idle CPU with the tab open is zero and there is no frame
 *    scheduler to leak on destroy(). prefers-reduced-motion kills the lot.
 * 7. A DRAGGED CARD STAYS PUT, including across the 15s poll — v1 re-anchored
 *    every card on every poll, which made dragging look broken. layout() skips
 *    targets for dropped cards; only "reset layout" releases them.
 */
(function () {
  'use strict';

  var NS = 'http://www.w3.org/2000/svg';

  // ---------------------------------------------------------------- topology
  // Mirrors app.py's stack dependency wiring. Exported so tests can assert
  // every id here exists in monitor.py's STACK_META. Edges into observability
  // are kept here (they are real dependencies) but never drawn — see rule 4.
  var TOPOLOGY = {
    'auth': ['identity', 'gateway'],
    'identity': ['runtime-orchestrator', 'runtime-research-agent'],
    'gateway': ['runtime-orchestrator', 'runtime-research-agent', 'observability'],
    'memory': ['runtime-orchestrator', 'observability'],
    'networking': ['runtime-orchestrator', 'runtime-code-agent', 'runtime-research-agent'],
    'security': ['memory'],
    // runtime-orchestrator to the two A2A agents is delegation, not deploy order
    'runtime-orchestrator': ['runtime-code-agent', 'runtime-research-agent', 'observability'],
    'runtime-code-agent': ['observability'],
    'runtime-research-agent': ['observability']
  };

  var LAYER_ORDER = ['foundation', 'identity', 'service', 'runtime', 'observability'];
  var LAYER_OF = {
    'networking': 'foundation', 'security': 'foundation',
    'auth': 'identity', 'identity': 'identity',
    'gateway': 'service', 'memory': 'service',
    'runtime-orchestrator': 'runtime', 'runtime-code-agent': 'runtime',
    'runtime-research-agent': 'runtime',
    'observability': 'observability'
  };
  // The stack NAME is the primary label on a card. A module id is a workshop
  // ordering hint, not an identity — v1 made it the only thing on the node and
  // the picture read as a bag of numbers.
  var NAME = {
    'networking': 'networking', 'security': 'security', 'auth': 'auth',
    'identity': 'identity', 'gateway': 'gateway', 'memory': 'memory',
    'runtime-orchestrator': 'orchestrator', 'runtime-code-agent': 'code agent',
    'runtime-research-agent': 'research agent', 'observability': 'observability'
  };
  var PLATFORM_SIDE = ['auth', 'identity', 'gateway', 'observability'];
  // identity AND observability exist on both sides: the workload account runs
  // its own token vault (that is what makes the trust pure OAuth) and its own
  // observability stack, because each side monitors what it runs (app.py).
  var WORKLOAD_SIDE = ['identity', 'observability', 'memory', 'runtime-orchestrator',
    'runtime-code-agent', 'runtime-research-agent', 'networking', 'security'];
  var RUNTIME_IDS = ['runtime-orchestrator', 'runtime-code-agent', 'runtime-research-agent'];
  // Fixed tiebreak for row ordering. Determinism needs a total order that does
  // not depend on object key iteration of the incoming JSON.
  var BASE_RANK = {};
  Object.keys(LAYER_OF).forEach(function (b, i) { BASE_RANK[b] = i; });

  // ------------------------------------------------------------------- state
  var root = null, legendEl = null, pane = null, viewport = null;
  var svg = null, edgeLayer = null, groupLayer = null, nodeLayer = null;
  var trayEl = null, trayLabel = null, vpcEl = null, guidanceEl = null, detailsEl = null;
  var nodes = [], edges = [], containers = [], trayBox = null;
  var elById = {}, edgeElById = {};
  var status = null, pendingStatus = null, guidanceText = '', warned = false;
  var mounted = false, selectedId = null;
  var view = { k: 1, tx: 0, ty: 0, userAdjusted: false };
  var drag = { node: null, dx: 0, dy: 0, moved: false };
  var pan = { on: false, moved: false, x: 0, y: 0 };
  var W = 900, H = 560, bounds = { x: 0, y: 0, w: 900, h: 560 };
  var listeners = [], timers = [], ro = null;
  // Card geometry is fixed; a small pane is handled by zooming the viewport,
  // not by shrinking type (that was the canvas version's compromise). v4 cards
  // are smaller and the gutters much airier RELATIVE TO THE CARD (gutter/card
  // 0.87 vs v3's 0.65, row gap/card height 0.46 vs 0.29), which is the way to
  // buy whitespace here: the drawing is auto-fitted, so widening the absolute
  // gutter past ~130 only zooms the whole picture out and the type with it.
  // Wide gutters are also what gives the beziers room to read as curves.
  var CW = 150, CH = 56, GUT = 130, VGAP = 26, PADX = 30, HEAD = 44;
  var MIN_K = 0.3, MAX_K = 2;

  function clamp(v, a, b) { return v < a ? a : (v > b ? b : v); }
  function r1(v) { return Math.round(v * 10) / 10; }

  // -------------------------------------------------------------- DOM helpers
  function el(tag, cls, parent) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (parent) parent.appendChild(e);
    return e;
  }
  function sv(tag, cls, parent) {
    var e = document.createElementNS(NS, tag);
    if (cls) e.setAttribute('class', cls);
    if (parent) parent.appendChild(e);
    return e;
  }
  function txt(e, s) { e.textContent = (s === null || s === undefined) ? '' : String(s); return e; }
  function at(e, k, v) { e.setAttribute(k, String(v)); return e; }
  function on(target, type, fn, opts) {
    if (!target || typeof target.addEventListener !== 'function') return;
    target.addEventListener(type, fn, opts || false);
    listeners.push([target, type, fn, opts || false]);
  }
  function later(fn, ms) { timers.push(setTimeout(fn, ms)); }

  // -------------------------------------------------------------- data → model
  function suffixOf(key, project, env) {
    var pre = (project || '') + '-' + (env || '') + '-';
    return key.indexOf(pre) === 0 ? key.slice(pre.length) : key;
  }

  /** Which side of a federated deployment the polled account sits on. */
  function polledRole(dep) {
    if (!dep) return null;
    if (dep.role === 'platform' || dep.role === 'workload') return dep.role;
    var acct = dep.polled_account || '';
    if (acct && acct === dep.platform_account) return 'platform';
    var wl = Array.isArray(dep.workload_accounts) ? dep.workload_accounts : [];
    if (acct && wl.indexOf(acct) >= 0) return 'workload';
    return null;
  }

  function buildModel(s) {
    // Both blocks are shape-checked, not just truthiness-checked: a string
    // `stacks` passes `||` and then Object.keys() walks its CHARACTERS, so a
    // malformed file drew a card per letter instead of falling back. The
    // try/catch in update() cannot catch that — nothing throws.
    var dep = (s && typeof s.deployment === 'object' && s.deployment) || {};
    var strategy = typeof dep.strategy === 'string' && dep.strategy ? dep.strategy : 'centralized';
    var federated = strategy === 'federated';
    var stacks = (s && typeof s.stacks === 'object' && s.stacks) || {};
    var project = (s && s.project) || '', env = (s && s.environment) || '';
    var role = federated ? polledRole(dep) : null;

    var seen = {};
    Object.keys(stacks).forEach(function (key) {
      var rec = stacks[key] || {};
      seen[suffixOf(key, project, env)] = { key: key, rec: rec };
    });

    var wanted = [];
    if (federated) {
      PLATFORM_SIDE.forEach(function (b) { wanted.push({ base: b, side: 'platform' }); });
      WORKLOAD_SIDE.forEach(function (b) { wanted.push({ base: b, side: 'workload' }); });
    } else {
      Object.keys(LAYER_OF).forEach(function (b) { wanted.push({ base: b, side: null }); });
    }
    // Unknown extra stacks still get a card rather than disappearing silently.
    Object.keys(seen).sort().forEach(function (b) {
      if (!LAYER_OF[b]) wanted.push({ base: b, side: federated ? (role || 'platform') : null, extra: true });
    });

    var byId = {};
    nodes.forEach(function (n) { byId[n.id] = n; });
    var next = [], nextIds = {}, flashes = [];

    wanted.forEach(function (w) {
      var id = w.side === 'workload' && PLATFORM_SIDE.indexOf(w.base) >= 0 ? w.base + '#workload' : w.base;
      if (nextIds[id]) return;
      nextIds[id] = true;
      // Only the polled side can be reported on. Anything on the other side of
      // a federation is unknown from here, full stop.
      var observed = !federated || !role || w.side === role;
      var hit = observed ? seen[w.base] : null;
      var rec = hit ? hit.rec : {};
      var n = byId[id] || { id: id, x: NaN, y: NaN, dropped: false, state: null };
      var st = observed ? (typeof rec.state === 'string' && rec.state ? rec.state : 'not-deployed') : 'unobserved';
      // One-shot flashes fire on the TRANSITION only, never on a steady state.
      if (n.state && n.state !== st && (st === 'deployed' || st === 'failed')) {
        flashes.push([n, st === 'deployed' ? 'rf-flash-ok' : 'rf-flash-bad']);
      }
      n.base = w.base;
      n.side = w.side;
      n.observed = observed;
      n.state = st;
      n.key = hit ? hit.key : (project && env ? project + '-' + env + '-' + w.base : w.base);
      n.module = rec.module || (w.extra ? '?' : '');
      n.team = rec.team || '';
      n.status = observed ? (rec.status || '') : '';
      n.description = rec.description || '';
      n.outputs = rec.outputs && typeof rec.outputs === 'object' ? rec.outputs : {};
      n.count = Array.isArray(rec.resources) ? rec.resources.length : 0;
      n.layer = LAYER_OF[w.base] || 'runtime';
      n.name = NAME[w.base] || w.base;
      if (w.side === 'workload' && w.base === 'identity') n.name = 'identity (local vault)';
      // Out of scope lives in the tray, never in a column.
      n.tray = n.state === 'not-applicable';
      next.push(n);
    });

    nodes = next;
    if (selectedId && !nextIds[selectedId]) selectedId = null;
    if (drag.node && next.indexOf(drag.node) < 0) drag.node = null;
    containers = buildContainers(strategy, dep, federated);
    edges = buildEdges(federated);
    layout();
    guidanceText = guidance();
    render();
    fitView();
    flashes.forEach(function (f) { flash(f[0], f[1]); });
  }

  function nodeFor(base, preferSide) {
    var hits = nodes.filter(function (n) { return n.base === base; });
    if (hits.length < 2) return hits[0] || null;
    var pref = hits.filter(function (n) { return n.side === preferSide; })[0];
    return pref || hits[0];
  }

  function buildEdges(federated) {
    var out = [], seen = {};
    Object.keys(TOPOLOGY).forEach(function (from) {
      (TOPOLOGY[from] || []).forEach(function (to) {
        // Observability is drawn as a per-card badge, not as edges: five lines
        // converging on one card was the single biggest source of crossings in
        // v1. TOPOLOGY stays complete because it mirrors app.py's real wiring;
        // the filter lives here so the map and the picture can disagree on
        // purpose, in one obvious place.
        if (to === 'observability') return;
        var a = nodeFor(from, null), b = nodeFor(to, null);
        var aOnly = nodes.filter(function (n) { return n.base === from; });
        var bOnly = nodes.filter(function (n) { return n.base === to; });
        if (aOnly.length > 1 && bOnly.length === 1) a = nodeFor(from, bOnly[0].side);
        if (bOnly.length > 1 && aOnly.length === 1) b = nodeFor(to, aOnly[0].side);
        if (!a || !b || a === b || a.tray || b.tray || a.side !== b.side) return;
        var k = a.id + '>' + b.id;
        if (seen[k]) return;
        seen[k] = true;
        out.push({ id: k, a: a, b: b, kind: 'dep' });
      });
    });
    if (federated) {
      // ONE trust edge, not one per platform service: the trust itself is
      // "workload vault exchanges platform Cognito M2M credentials". Drawing it
      // from gateway as well repeated the same fact and cost a crossing.
      var vault = nodes.filter(function (n) { return n.base === 'identity' && n.side === 'workload'; })[0];
      var auth = nodes.filter(function (n) { return n.base === 'auth' && n.side === 'platform'; })[0];
      if (auth && vault) {
        out.push({
          id: 'trust', a: auth, b: vault, kind: 'trust',
          label: 'OAuth (no cross-account IAM)'
        });
      }
    }
    return out;
  }

  function buildContainers(strategy, dep, federated) {
    var polled = dep.polled_account || (status && status.account) || 'unknown';
    if (federated) {
      var role = polledRole(dep);
      var wl = (Array.isArray(dep.workload_accounts) ? dep.workload_accounts : []).join(', ') || 'unknown';
      return [
        {
          side: 'platform', title: 'Account ' + (dep.platform_account || 'unknown') + ' · federated platform',
          caption: role === 'platform' ? 'polled from here' : 'not observed from this account',
          polled: role === 'platform'
        },
        {
          side: 'workload', title: 'Account ' + wl + ' · federated workload',
          caption: role === 'workload' ? 'polled from here' : 'not observed from this account',
          polled: role === 'workload'
        }
      ];
    }
    if (strategy === 'distributed') {
      return [{
        side: null, title: 'Account ' + polled + ' · distributed',
        caption: 'each account runs its own full copy — showing ' + polled, polled: true
      }];
    }
    return [{ side: null, title: 'Account ' + polled + ' · centralized', caption: 'polled from here', polled: true }];
  }

  // ------------------------------------------------------------------- layout
  /* Deterministic orthogonal layout. Columns = layers, rows = cards, one pass
   * of the barycentre heuristic to order rows. No randomness, no simulation:
   * a card only moves when the computed slot moves, and then CSS transitions it.
   *
   * ponytail: ONE barycentre pass, not full Sugiyama. Ceiling — long edges
   * (networking → runtimes) still pass behind intermediate cards; they are drawn
   * under the opaque cards so it reads cleanly. Upgrade path is dummy nodes per
   * proper Sugiyama, ~300 lines for a 10-card diagram, so: not yet.
   */
  function layout() {
    var boxW = PADX * 2 + LAYER_ORDER.length * CW + (LAYER_ORDER.length - 1) * GUT;
    var y = 28;

    containers.forEach(function (c) {
      var cols = columnsFor(c.side);
      var tallest = 0;
      cols.forEach(function (list) {
        var h = list.length * CH + Math.max(0, list.length - 1) * VGAP;
        if (h > tallest) tallest = h;
      });
      var boxH = HEAD + Math.max(CH, tallest) + PADX;
      c.box = { x: 0, y: y, w: boxW, h: boxH };
      // Each column is vertically centred as a group inside the box body, so a
      // column of one sits on the midline instead of hugging the top.
      var mid = c.box.y + HEAD + Math.max(CH, tallest) / 2;
      cols.forEach(function (list, ci) {
        var top = mid - (list.length * CH + Math.max(0, list.length - 1) * VGAP) / 2;
        list.forEach(function (n, ri) {
          n.col = ci; n.row = ri; n.w = CW; n.h = CH;
          place(n, colX(ci), top + ri * (CH + VGAP));
        });
      });
      y += boxH + 34;
    });

    layoutTray(boxW, y);
    laneEdges();
  }

  function colX(ci) { return PADX + ci * (CW + GUT); }

  /** In-scope cards of one side, grouped into layer columns, rows ordered by a
   *  single barycentre pass over already-placed predecessors. */
  function columnsFor(side) {
    var mine = nodes.filter(function (n) { return n.side === side && !n.tray; });
    var cols = LAYER_ORDER.map(function (layer) {
      return mine.filter(function (n) { return n.layer === layer; })
        .sort(function (a, b) { return (BASE_RANK[a.base] || 99) - (BASE_RANK[b.base] || 99); });
    });
    var rowOf = {};
    cols.forEach(function (list, ci) {
      if (ci > 0) {
        list.forEach(function (n, i) {
          var sum = 0, k = 0;
          edges.forEach(function (e) {
            if (e.kind !== 'dep' || e.b !== n) return;
            if (typeof rowOf[e.a.id] === 'number') { sum += rowOf[e.a.id]; k++; }
          });
          n._bary = k ? sum / k : i;
          n._i = i;
        });
        list.sort(function (a, b) { return (a._bary - b._bary) || (a._i - b._i); });
      }
      list.forEach(function (n, i) { rowOf[n.id] = i; });
    });
    return cols;
  }

  /** Out-of-scope cards get a dim strip along the bottom. They are laid out
   *  last and never influence a column, so enabling a module in platform.yaml
   *  is the only thing that can move the main diagram. */
  function layoutTray(boxW, y) {
    var list = nodes.filter(function (n) { return n.tray; });
    trayBox = null;
    if (!list.length) return;
    list.sort(function (a, b) { return (BASE_RANK[a.base] || 99) - (BASE_RANK[b.base] || 99); });
    var w = 136, h = 40, gap = 14;   // room for a smaller icon tile + one line
    var perRow = Math.max(1, Math.floor((boxW - PADX * 2 + gap) / (w + gap)));
    var rows = Math.ceil(list.length / perRow);
    trayBox = { x: 0, y: y, w: boxW, h: 30 + rows * (h + gap) };
    list.forEach(function (n, i) {
      var r = Math.floor(i / perRow), c = i % perRow;
      n.w = w; n.h = h; n.col = -1; n.row = r;
      place(n, PADX + c * (w + gap), y + 26 + r * (h + gap));
    });
  }

  /** A card the user dropped keeps its own position across update() calls —
   *  re-anchoring on the next poll was v1's most annoying bug (drags looked
   *  like they did nothing). Everything else snaps to the computed slot and
   *  lets the CSS transition carry it there. */
  function place(n, x, y) {
    if (n.dropped) return;
    n.x = x; n.y = y;
  }

  /** Lanes exist only for the edges that have to loop. A forward edge derives
   *  its whole shape from dx (see bezier), so it needs no lane; a same-column
   *  edge (auth→identity, orchestrator→agents) bulges into the gutter to the
   *  right, and parallel bulges of the same size would sit on top of each other.
   *  Lanes are assigned in a deterministic order (source row, then target row)
   *  so the same model always draws the same picture. */
  function laneEdges() {
    var groups = {};
    edges.forEach(function (e) {
      if (e.kind === 'trust') return;
      e.back = e.b.col <= e.a.col;
      if (!e.back) return;
      e.gut = Math.max(e.a.col, e.b.col);
      (groups[e.gut] = groups[e.gut] || []).push(e);
    });
    Object.keys(groups).forEach(function (g) {
      var list = groups[g];
      list.sort(function (p, q) { return (p.a.row - q.a.row) || (p.b.row - q.b.row); });
      list.forEach(function (e, i) {
        e.lane = colX(e.gut) + CW + GUT * (i + 1) / (list.length + 1);
      });
    });
  }

  function guidance() {
    var scope = nodes.filter(function (n) { return n.observed && n.state !== 'not-applicable'; });
    var busy = scope.filter(function (n) { return n.state === 'in-progress'; })[0];
    if (busy) return busy.name + ' deploying — the runtime build typically takes ~7 min';
    var bad = scope.filter(function (n) { return n.state === 'failed'; })[0];
    if (bad) return bad.name + ' failed — check its CloudFormation events';
    var todo = scope.filter(function (n) { return n.state === 'not-deployed'; }).length;
    if (todo) return todo + ' stack(s) not deployed yet';
    return 'All ' + scope.length + ' stacks deployed';
  }

  /** A card is badged when its stack ships logs+traces to an observability
   *  stack that is actually deployed on the same side. Badging an undeployed
   *  observability stack would claim delivery that cannot be happening. */
  function badged(n) {
    if (!n.observed || n.tray || n.state !== 'deployed') return false;
    if ((TOPOLOGY[n.base] || []).indexOf('observability') < 0) return false;
    return nodes.some(function (o) {
      return o.base === 'observability' && o.side === n.side && o.state === 'deployed';
    });
  }

  // ------------------------------------------------------------------- render
  function render() {
    if (!mounted) return;
    renderGroups();
    renderTray();
    renderNodes();
    renderVpc();
    measureContent();
    renderEdges();
    txt(guidanceEl, guidanceText);
    renderDetails();
  }

  function measureContent() {
    var x0 = 0, y0 = 0, x1 = 320, y1 = 240;
    function add(x, y, w, h) {
      x0 = Math.min(x0, x); y0 = Math.min(y0, y);
      x1 = Math.max(x1, x + w); y1 = Math.max(y1, y + h);
    }
    containers.forEach(function (c) { if (c.box) add(c.box.x, c.box.y - 18, c.box.w, c.box.h + 18); });
    if (trayBox) add(trayBox.x, trayBox.y, trayBox.w, trayBox.h);
    nodes.forEach(function (n) { if (isFinite(n.x)) add(n.x - 8, n.y - 8, n.w + 16, n.h + 16); });
    bounds = { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
    // The edge <svg> sits at the viewport origin and spans the whole layout, so
    // path coordinates ARE layout coordinates — no per-edge offset maths.
    // overflow:visible in the CSS covers the ±8px the layout can go negative.
    at(svg, 'width', Math.max(0, x1));
    at(svg, 'height', Math.max(0, y1));
  }

  function renderGroups() {
    var kept = 0;
    containers.forEach(function (c) {
      var e = groupLayer.children[kept];
      if (!e) {
        e = el('div', 'rf-group', groupLayer);
        c.titleEl = txt(el('div', 'rf-group-title', e), '');
        c.capEl = txt(el('div', 'rf-group-caption', e), '');
        c.labelsEl = el('div', 'rf-layer-labels', e);
        e._c = c;
      } else {
        c.titleEl = e.children[0]; c.capEl = e.children[1]; c.labelsEl = e.children[2];
      }
      kept++;
      e.className = 'rf-group' + (c.polled ? '' : ' rf-group--unobserved');
      e.style.left = c.box.x + 'px';
      e.style.top = c.box.y + 'px';
      e.style.width = c.box.w + 'px';
      e.style.height = c.box.h + 'px';
      txt(c.titleEl, c.title);
      txt(c.capEl, c.caption);
      if (!c.labelsEl.children.length) {
        LAYER_ORDER.forEach(function (l, i) {
          var t = txt(el('div', 'rf-layer-label', c.labelsEl), l);
          t.style.left = colX(i) + 'px';
          t.style.width = CW + 'px';
        });
      }
    });
    while (groupLayer.children.length > kept) {
      groupLayer.removeChild(groupLayer.children[groupLayer.children.length - 1]);
    }
  }

  function renderTray() {
    var show = !!trayBox;
    trayEl.style.display = show ? 'block' : 'none';
    if (!show) return;
    trayEl.style.left = trayBox.x + 'px';
    trayEl.style.top = trayBox.y + 'px';
    trayEl.style.width = trayBox.w + 'px';
    trayEl.style.height = trayBox.h + 'px';
    txt(trayLabel, 'Not in this profile — enable in platform.yaml');
  }

  function renderVpc() {
    // Only claim a VPC when the networking stack is actually deployed — drawing
    // it unconditionally would assert infrastructure that is not there.
    var net = nodes.filter(function (n) {
      return n.base === 'networking' && n.observed && n.state === 'deployed';
    })[0];
    var rts = net ? nodes.filter(function (n) {
      return RUNTIME_IDS.indexOf(n.base) >= 0 && n.side === net.side && !n.tray;
    }) : [];
    if (!rts.length) { vpcEl.style.display = 'none'; return; }
    var x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9;
    rts.forEach(function (n) {
      x0 = Math.min(x0, n.x); y0 = Math.min(y0, n.y);
      x1 = Math.max(x1, n.x + n.w); y1 = Math.max(y1, n.y + n.h);
    });
    var m = 16;
    vpcEl.style.display = 'block';
    vpcEl.style.left = (x0 - m) + 'px';
    vpcEl.style.top = (y0 - m) + 'px';
    vpcEl.style.width = (x1 - x0 + m * 2) + 'px';
    vpcEl.style.height = (y1 - y0 + m * 2) + 'px';
  }

  /* One glyph per layer, built from primitives (rect/circle/polygon) rather
   * than hand-authored path data: shapes I can reason about exactly are the
   * right call when the only way to check a `d` string is to look at it.
   * Colour comes from CSS (fill/stroke: currentColor) so the pale not-deployed
   * tiles can swap white for grey in one rule instead of in JS. */
  var GLYPH = {
    // three slabs — the base everything else sits on (networking, security)
    foundation: [['rect', { x: 3, y: 3.4, width: 10, height: 2.2, rx: 0.7 }],
      ['rect', { x: 3, y: 6.9, width: 10, height: 2.2, rx: 0.7 }],
      ['rect', { x: 3, y: 10.4, width: 10, height: 2.2, rx: 0.7 }]],
    // head + shoulders — who is calling (auth, token vault)
    identity: [['circle', { cx: 8, cy: 5.2, r: 2.4 }],
      ['rect', { x: 3.2, y: 9.2, width: 9.6, height: 4.6, rx: 2.3 }]],
    // two boxes joined by a link — a gateway/memory between callers (service)
    service: [['rect', { x: 2, y: 4, width: 4, height: 8, rx: 1.1 }],
      ['rect', { x: 10, y: 4, width: 4, height: 8, rx: 1.1 }],
      ['rect', { x: 6, y: 7.2, width: 4, height: 1.6, rx: 0.6 }]],
    // a play triangle — something that runs
    runtime: [['polygon', { points: '5,3.2 12.4,8 5,12.8' }]],
    // ring + pupil — something watching
    observability: [['circle', { cx: 8, cy: 8, r: 4.9, fill: 'none', stroke: 'currentColor', 'stroke-width': 1.6 }],
      ['circle', { cx: 8, cy: 8, r: 1.9 }]]
  };

  function glyph(layer, parent) {
    var g = sv('svg', 'rf-glyph', parent);
    at(g, 'viewBox', '0 0 16 16');
    at(g, 'aria-hidden', 'true');       // the card's aria-label already says it
    at(g, 'focusable', 'false');
    (GLYPH[layer] || GLYPH.runtime).forEach(function (shape) {
      var s = sv(shape[0], null, g);
      Object.keys(shape[1]).forEach(function (k) { at(s, k, shape[1][k]); });
    });
    return g;
  }

  /* Card DOM, deliberately flat:
   *   .rf-node[tabindex][role=button]
   *     .rf-tile   > svg.rf-glyph      state colour + layer glyph
   *     .rf-text   > .rf-name/.rf-sub  two ellipsised lines, nothing else
   *     .rf-badge                      observability delivery, when earned
   * No module pill and no handle dots: the pill was a third line of type on a
   * two-line card and the module number lives in the details panel, where
   * someone actually looking for it will look. */
  function makeNode(n) {
    var e = el('div', 'rf-node');
    at(e, 'tabindex', '0');
    at(e, 'role', 'button');
    e._rf = n.id;
    e._tile = el('span', 'rf-tile', e);
    glyph(n.layer, e._tile);
    var text = el('div', 'rf-text', e);
    e._name = el('div', 'rf-name', text);
    e._sub = el('div', 'rf-sub', text);
    e._badge = at(txt(el('span', 'rf-badge', e), '●'), 'title', 'logs + traces delivered');
    return e;
  }

  function syncNode(n, e) {
    var parent = n.tray ? trayEl : nodeLayer;
    if (e.parentNode !== parent) parent.appendChild(e);
    e.className = 'rf-node rf-node--' + n.state +
      (n.tray ? ' rf-node--tray' : '') +
      (n.observed ? '' : ' rf-node--unobserved') +
      (n.id === selectedId ? ' is-selected' : '') +
      (n.dropped ? ' is-pinned' : '');
    e.style.width = n.w + 'px';
    e.style.height = n.h + 'px';
    var ox = n.tray && trayBox ? trayBox.x : 0, oy = n.tray && trayBox ? trayBox.y : 0;
    e.style.transform = 'translate(' + r1(n.x - ox) + 'px,' + r1(n.y - oy) + 'px)';
    txt(e._name, n.name);
    txt(e._sub, !n.observed ? 'not observed' : (n.tray ? 'not in scope' : n.count + ' resources'));
    e._badge.style.display = badged(n) ? 'block' : 'none';
    var label = n.name + ' — ' + (n.observed ? n.state : 'not observed from this account');
    at(e, 'aria-label', label);
    at(e, 'title', label);
  }

  function renderNodes() {
    var want = {};
    nodes.forEach(function (n) {
      want[n.id] = true;
      var e = elById[n.id];
      if (!e) { e = makeNode(n); elById[n.id] = e; }
      n.el = e;
      syncNode(n, e);
    });
    Object.keys(elById).forEach(function (id) {
      if (want[id]) return;
      var e = elById[id];
      if (e.parentNode) e.parentNode.removeChild(e);
      delete elById[id];
    });
  }

  /** One cubic bezier per edge, leaving the source horizontally and arriving
   *  horizontally — the whole reason the picture reads as flow.
   *
   *  Forward (target in a later column): both control points sit on the
   *  endpoints' own y, offset horizontally by half the run,
   *    C (sx+c, sy) (ex-c, ey) (ex, ey)    c = clamp(|dx| * 0.5, 34, 130)
   *  The floor keeps a short hop from collapsing into a straight line; the cap
   *  stops the long networking→runtime run from ballooning into an S. Because c
   *  depends only on dx, several edges out of one card leave at the same angle
   *  and separate as they travel (fan-out), and several edges into one card
   *  converge on the same tangent (merge) — no lane bookkeeping needed.
   *
   *  Backward / same-column (auth→identity, orchestrator→its A2A agents): there
   *  is no forward room, so both control points bulge to the RIGHT of both
   *  cards by the edge's lane offset and the curve comes back into the target's
   *  right edge. Lanes keep parallel loops off each other, deterministically.
   *
   *  Straight is a bezier too (collinear controls): never emit an L/Q elbow,
   *  the smoothstep geometry is exactly what v4 is replacing. */
  function bezier(sx, sy, ex, ey, bulge) {
    var c1x, c2x;
    if (bulge) { c1x = sx + bulge; c2x = ex + bulge; }
    else {
      var c = clamp(Math.abs(ex - sx) * 0.5, 34, 130);
      c1x = sx + c; c2x = ex - c;
    }
    return 'M' + r1(sx) + ',' + r1(sy) +
      'C' + r1(c1x) + ',' + r1(sy) + ' ' + r1(c2x) + ',' + r1(ey) + ' ' + r1(ex) + ',' + r1(ey);
  }

  function edgeKind(e) {
    if (e.kind === 'trust') return 'trust';
    if (!e.a.observed || !e.b.observed) return 'unobserved';
    if (e.b.state === 'in-progress') return 'building';
    if (e.a.state === 'deployed' && e.b.state === 'deployed') return 'live';
    return 'default';
  }

  function renderEdges() {
    var want = {};
    edges.forEach(function (e) {
      want[e.id] = true;
      var rec = edgeElById[e.id];
      if (!rec) {
        rec = { path: sv('path', 'rf-edge', edgeLayer), label: null };
        edgeElById[e.id] = rec;
      }
      var kind = edgeKind(e), d;
      if (e.kind === 'trust') {
        // Cross-account: one dashed accent run down between the two boxes.
        // Vertical, so the control points are offset in y instead of x.
        var tsx = e.a.x + e.a.w / 2, tsy = e.a.y + e.a.h;
        var tex = e.b.x + e.b.w / 2, tey = e.b.y;
        var tc = Math.max(24, Math.abs(tey - tsy) * 0.45);
        d = 'M' + r1(tsx) + ',' + r1(tsy) +
          'C' + r1(tsx) + ',' + r1(tsy + tc) + ' ' + r1(tex) + ',' + r1(tey - tc) +
          ' ' + r1(tex) + ',' + r1(tey);
      } else {
        var sx = e.a.x + e.a.w, sy = e.a.y + e.a.h / 2;
        d = bezier(sx, sy,
          e.back ? e.b.x + e.b.w : e.b.x, e.b.y + e.b.h / 2,
          e.back ? Math.max(28, (e.lane || sx + 40) - sx) : 0);
      }
      at(rec.path, 'd', d);
      at(rec.path, 'class', 'rf-edge rf-edge--' + kind);
      at(rec.path, 'marker-end', 'url(#rf-mk-' + kind + ')');
      if (e.label) {
        if (!rec.label) rec.label = sv('text', 'rf-edge-label', edgeLayer);
        at(rec.label, 'x', r1((e.a.x + e.a.w / 2 + e.b.x + e.b.w / 2) / 2));
        at(rec.label, 'y', r1((e.a.y + e.a.h + e.b.y) / 2 - 6));
        txt(rec.label, e.label);
      }
    });
    Object.keys(edgeElById).forEach(function (id) {
      if (want[id]) return;
      var rec = edgeElById[id];
      [rec.path, rec.label].forEach(function (x) { if (x && x.parentNode) x.parentNode.removeChild(x); });
      delete edgeElById[id];
    });
  }

  /** One-shot arrival/failure feedback. CSS owns the animation; the class is
   *  removed on animationend so a later transition can fire it again. */
  function flash(n, cls) {
    var e = n.el;
    if (!e || !e.classList) return;
    e.classList.add(cls);
    var done = function () { if (e.classList) e.classList.remove(cls); };
    if (typeof e.addEventListener === 'function') e.addEventListener('animationend', done, { once: true });
    later(done, 1400);   // belt and braces: reduced-motion never fires animationend
  }

  // ------------------------------------------------------------------ details
  function row(parent, k, v) {
    var r = el('div', 'rf-row', parent);
    txt(el('span', 'rf-row-k', r), k);
    txt(el('span', 'rf-row-v', r), v);
  }

  function renderDetails() {
    var n = selectedId ? nodes.filter(function (x) { return x.id === selectedId; })[0] : null;
    detailsEl.innerHTML = '';
    detailsEl.style.display = n ? 'block' : 'none';
    if (!n) return;
    // Real DOM text, so a workshop attendee can select and copy an ARN out of
    // the diagram — impossible in the canvas version, and half the reason for v3.
    txt(el('div', 'rf-details-title', detailsEl), n.key);
    row(detailsEl, 'module', (n.module || '?') + (n.team ? ' · ' + n.team + ' team' : ''));
    row(detailsEl, 'state', n.observed
      ? n.state + (n.status ? ' (' + n.status + ')' : '')
      : 'not observed from this account');
    if (n.observed) row(detailsEl, 'resources', String(n.count));
    if (n.description) row(detailsEl, 'about', n.description);
    Object.keys(n.outputs).slice(0, 6).forEach(function (k) {
      row(detailsEl, k, String(n.outputs[k]));
    });
  }

  // -------------------------------------------------------------------- view
  function applyView() {
    viewport.style.transform = 'translate(' + r1(view.tx) + 'px,' + r1(view.ty) + 'px) scale(' + r1(view.k * 100) / 100 + ')';
  }

  function measurePane() {
    W = Math.max(320, (pane && pane.clientWidth) || (root && root.clientWidth) || 900);
    H = Math.max(240, (pane && pane.clientHeight) || (root && root.clientHeight) || 560);
  }

  /** Centre and, if needed, shrink the whole drawing. Skipped once the user has
   *  panned or zoomed — their view wins until they hit fit/reset or double-click. */
  function fitView(force) {
    if (view.userAdjusted && !force) { applyView(); return; }
    measurePane();
    var pad = 24;
    view.k = clamp(Math.min((W - pad * 2) / bounds.w, (H - pad * 2) / bounds.h), MIN_K, MAX_K);
    view.tx = (W - bounds.w * view.k) / 2 - bounds.x * view.k;
    view.ty = (H - bounds.h * view.k) / 2 - bounds.y * view.k;
    view.userAdjusted = false;
    applyView();
  }

  function zoomTo(k, cx, cy) {
    k = clamp(k, MIN_K, MAX_K);
    view.tx = cx - (cx - view.tx) * (k / view.k);
    view.ty = cy - (cy - view.ty) * (k / view.k);
    view.k = k;
    view.userAdjusted = true;
    applyView();
  }

  // -------------------------------------------------------------- interaction
  function rect(e) {
    try { return e.getBoundingClientRect(); } catch (err) { return { left: 0, top: 0 }; }
  }

  function paneXY(ev) {
    var r = rect(pane);
    return { x: (ev.clientX || 0) - (r.left || 0), y: (ev.clientY || 0) - (r.top || 0) };
  }

  function worldXY(ev) {
    var p = paneXY(ev);
    return { x: (p.x - view.tx) / view.k, y: (p.y - view.ty) / view.k };
  }

  /** Walk up to the card element. No closest()/dataset so the same code path is
   *  exercised by the headless driver as by the browser. */
  function nodeAt(target) {
    for (var e = target; e; e = e.parentNode) {
      if (e._rf) return nodes.filter(function (n) { return n.id === e._rf; })[0] || null;
    }
    return null;
  }

  function select(id) {
    selectedId = id;
    nodes.forEach(function (n) { if (n.el) syncNode(n, n.el); });
    renderDetails();
  }

  /** Overlays (details panel, controls) live inside the pane so they stay put
   *  while it pans, which means their clicks bubble here. Without this
   *  guard, selecting text in the details panel panned the view and the mouseup
   *  deselected the very card you were copying an ARN out of. */
  function inOverlay(target) {
    for (var e = target; e; e = e.parentNode) { if (e._overlay) return true; }
    return false;
  }

  function onDown(ev) {
    if (inOverlay(ev.target)) return;
    var hit = nodeAt(ev.target);
    drag.moved = false;
    if (hit) {
      var w = worldXY(ev);
      drag.node = hit;
      drag.dx = w.x - hit.x;
      drag.dy = w.y - hit.y;
      if (hit.el && hit.el.classList) hit.el.classList.add('is-dragging');
      if (ev.preventDefault) ev.preventDefault();   // no text selection mid-drag
      return;
    }
    var p = paneXY(ev);
    pan.on = true; pan.moved = false; pan.x = p.x; pan.y = p.y;
    if (pane.classList) pane.classList.add('is-panning');
  }

  function onMove(ev) {
    if (drag.node) {
      var w = worldXY(ev);
      var n = drag.node;
      n.x = w.x - drag.dx;
      n.y = w.y - drag.dy;
      drag.moved = true;
      syncNode(n, n.el);
      return;
    }
    if (!pan.on) return;
    var p = paneXY(ev);
    view.tx += p.x - pan.x; view.ty += p.y - pan.y;
    pan.x = p.x; pan.y = p.y;
    pan.moved = true;
    view.userAdjusted = true;          // a hand-panned view is not auto-fitted again
    applyView();
  }

  function onUp() {
    var n = drag.node;
    if (n) {
      if (n.el && n.el.classList) n.el.classList.remove('is-dragging');
      if (drag.moved) { n.dropped = true; syncNode(n, n.el); }
      else { select(n.id === selectedId ? null : n.id); }
      drag.node = null;
      measureContent();
      renderEdges();
    } else if (pan.on && !pan.moved) {
      select(null);                    // a click on empty pane, not the end of a pan
    }
    pan.on = false;
    if (pane.classList) pane.classList.remove('is-panning');
  }

  function onWheel(ev) {
    if (ev.preventDefault) ev.preventDefault();
    var p = paneXY(ev);
    zoomTo(view.k * Math.exp(-(ev.deltaY || 0) * 0.0015), p.x, p.y);
  }

  function onDblClick() { fitView(true); }

  function onKey(ev) {
    var n = nodeAt(ev.target);
    if (!n) return;
    if (ev.key === 'Enter' || ev.key === ' ' || ev.key === 'Spacebar') {
      if (ev.preventDefault) ev.preventDefault();
      select(n.id === selectedId ? null : n.id);
    }
  }

  function resetLayout() {
    nodes.forEach(function (n) { n.dropped = false; });
    layout();
    render();
    fitView(true);
  }

  function button(parent, label, fn, glyph) {
    var b = el('button', 'rf-btn', parent);
    at(b, 'type', 'button');
    at(b, 'aria-label', label);
    at(b, 'title', label);
    txt(b, glyph);
    on(b, 'click', fn);
    return b;
  }

  // ------------------------------------------------------------------ legend
  var LEGEND = [
    ['deployed', 'green', 'CloudFormation reports the stack complete'],
    ['in progress', 'amber', 'deploying now — border pulses, incoming edges march'],
    ['failed', 'red', 'failed or rolled back'],
    ['not deployed', 'text3', 'in scope, not there yet'],
    ['not in scope', 'gray', 'not applicable for this configuration — shown in the bottom tray'],
    ['not observed', 'gray', 'lives in another account — this dashboard polls only one']
  ];

  function renderLegend() {
    if (!legendEl || typeof legendEl.innerHTML !== 'string') return;
    // Swatches are rounded squares because that is now what a card shows: the
    // state colour lives in the icon tile, not in a border.
    var rows = LEGEND.map(function (r) {
      var dashed = (r[0] === 'not observed' || r[0] === 'not deployed');
      return '<div style="display:flex;gap:8px;align-items:baseline;margin:2px 0">' +
        '<span style="flex:0 0 10px;height:10px;border-radius:3px;margin-top:5px;' +
        (dashed ? 'border:1px dashed var(--' + r[1] + ')' : 'background:var(--' + r[1] + ')') + '"></span>' +
        '<span style="color:var(--text2);font-size:.8rem"><b style="color:var(--text)">' + r[0] +
        '</b> — ' + r[2] + '</span></div>';
    }).join('');
    legendEl.innerHTML = rows +
      '<div style="display:flex;gap:8px;align-items:baseline;margin:2px 0">' +
      '<span style="flex:0 0 10px;height:10px;border-radius:50%;margin-top:5px;background:var(--accent2)"></span>' +
      '<span style="color:var(--text2);font-size:.8rem"><b style="color:var(--text)">badge</b> — ' +
      'logs + traces delivered to the observability stack (drawn as a badge, not as edges, ' +
      'so the diagram stays readable)</span></div>' +
      '<div style="margin-top:8px;color:var(--text3);font-size:.75rem;line-height:1.5">' +
      'The colour is in each card\u2019s <b>icon square</b>; the glyph says which layer it ' +
      'belongs to (base, identity, service, runtime, watching).<br>' +
      'This reflects <b>CloudFormation status on a ~15s poll</b>, not live traffic — there is ' +
      'no request-level data here.<br>' +
      'Drag a card to move it — it sticks where you drop it, including across polls. ' +
      'Click a card to select it and read its outputs · click the background to deselect · ' +
      'drag the background to pan · scroll to zoom · zoom, fit and reset buttons bottom-left · ' +
      'double-click the background to fit.</div>';
  }

  // ------------------------------------------------------------------ sizing
  function onResize() {
    measurePane();
    fitView();
  }

  // ---------------------------------------------------------------- scaffold
  function markers() {
    var defs = sv('defs', null, svg);
    ['default', 'live', 'building', 'trust', 'unobserved'].forEach(function (kind) {
      var m = sv('marker', 'rf-mk rf-mk--' + kind, defs);
      at(m, 'id', 'rf-mk-' + kind);
      at(m, 'viewBox', '0 0 8 8');
      at(m, 'refX', '7');
      at(m, 'refY', '4');
      at(m, 'markerWidth', '5');
      at(m, 'markerHeight', '5');
      at(m, 'orient', 'auto');
      at(sv('path', null, m), 'd', 'M0,0 L8,4 L0,8 z');
    });
  }

  function build() {
    root.innerHTML = '';
    pane = el('div', 'rf-pane', root);
    viewport = el('div', 'rf-viewport', pane);
    groupLayer = el('div', 'rf-layer rf-groups', viewport);
    trayEl = el('div', 'rf-tray', viewport);
    trayLabel = el('div', 'rf-tray-label', trayEl);
    vpcEl = el('div', 'rf-vpc', viewport);
    txt(el('div', 'rf-vpc-label', vpcEl), 'VPC (private runtimes)');
    svg = sv('svg', 'rf-edges', viewport);
    markers();
    edgeLayer = sv('g', 'rf-edge-layer', svg);
    nodeLayer = el('div', 'rf-layer rf-nodes', viewport);

    guidanceEl = el('div', 'rf-guidance', pane);
    detailsEl = el('div', 'rf-details', pane);
    detailsEl.style.display = 'none';
    detailsEl._overlay = true;

    // Bare icon buttons in a row, bottom-left, with the guidance line beside
    // them. "reset layout" earns its place even though the reference has no
    // equivalent: dragging pins a card forever, so without it there is no way
    // back to the computed layout.
    var ctrl = el('div', 'rf-controls', pane);
    ctrl._overlay = true;
    at(ctrl, 'role', 'group');
    at(ctrl, 'aria-label', 'diagram controls');
    button(ctrl, 'zoom in', function () { zoomTo(view.k * 1.25, W / 2, H / 2); }, '+');
    button(ctrl, 'zoom out', function () { zoomTo(view.k / 1.25, W / 2, H / 2); }, '−');
    button(ctrl, 'fit view', function () { fitView(true); }, '⤢');
    button(ctrl, 'reset layout', resetLayout, '⟲');
  }

  function bind() {
    on(pane, 'mousedown', onDown);
    on(pane, 'wheel', onWheel, { passive: false });
    on(pane, 'dblclick', onDblClick);
    on(pane, 'keydown', onKey);
    // Move/up live on window so a fast drag that leaves the pane still tracks
    // and still terminates — a stuck drag was the worst v1 interaction bug.
    if (typeof window !== 'undefined') {
      on(window, 'mousemove', onMove);
      on(window, 'mouseup', onUp);
    }
    if (typeof ResizeObserver === 'function') {
      ro = new ResizeObserver(onResize);
      ro.observe(root);
    } else if (typeof window !== 'undefined') {
      on(window, 'resize', onResize);
    }
  }

  // --------------------------------------------------------------------- API
  function mount(containerEl, legendElement) {
    if (mounted) destroy();                                     // mount() twice is safe
    if (!containerEl || typeof containerEl.appendChild !== 'function') return;
    root = containerEl;
    legendEl = legendElement || null;
    build();
    bind();
    mounted = true;
    measurePane();
    renderLegend();
    // mount() before any poll is normal (the tab can be opened first): render an
    // empty pane now and let the queued or next update() fill it.
    if (pendingStatus) { var q = pendingStatus; pendingStatus = null; update(q); }
    else { render(); fitView(true); }
  }

  function update(statusJson) {
    if (!statusJson || typeof statusJson !== 'object') return;
    if (!mounted) { pendingStatus = statusJson; return; }        // tab not open yet
    var keep = nodes;
    try {
      status = statusJson;
      buildModel(statusJson);
      renderLegend();
    } catch (e) {
      // A malformed status.json must not kill the tab: keep the last good render
      // and warn once, so a broken poller does not spam the console every 15s.
      nodes = keep;
      if (!warned && typeof console !== 'undefined' && console.warn) {
        warned = true;
        console.warn('ArchGraph: bad status.json, keeping last good render', e);
      }
    }
  }

  function destroy() {
    mounted = false;
    listeners.forEach(function (l) {
      try { l[0].removeEventListener(l[1], l[2], l[3]); } catch (e) { /* gone already */ }
    });
    listeners = [];
    timers.forEach(function (t) { clearTimeout(t); });
    timers = [];
    if (ro && typeof ro.disconnect === 'function') { try { ro.disconnect(); } catch (e2) { /* gone */ } }
    ro = null;
    if (root) root.innerHTML = '';
    elById = {}; edgeElById = {};
    nodes.forEach(function (n) { n.el = null; });
    root = null; legendEl = null; pane = null; viewport = null; svg = null;
    edgeLayer = null; groupLayer = null; nodeLayer = null; trayEl = null;
    trayLabel = null; vpcEl = null; guidanceEl = null; detailsEl = null;
    drag.node = null; pan.on = false;
    // Card positions and `dropped` flags are deliberately KEPT: coming back to
    // the tab should show the platform where you left it.
  }

  window.ArchGraph = { mount: mount, update: update, destroy: destroy, TOPOLOGY: TOPOLOGY };
})();
