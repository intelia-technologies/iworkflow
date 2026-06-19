# ruff: noqa: E402
import json
import http.server
import socketserver
import time
from pathlib import Path
from typing import Any

# HTML Dashboard template
HTML_DASHBOARD = """<!DOCTYPE html>
<html lang="es" class="h-full bg-slate-50">
<head>
  <meta charset="UTF-8">
  <title>iworkflow Live Dashboard</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script type="module">
    import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.esm.min.mjs';
    mermaid.initialize({
      startOnLoad: false,
      securityLevel: 'loose',
      theme: 'neutral'
    });
    window.mermaid = mermaid;
  </script>
  <style>
    .mermaid svg {
      max-width: 100%;
      height: auto;
    }
    /* Mermaid status classes */
    .node.pending rect { fill: #f8fafc !important; stroke: #cbd5e1 !important; stroke-width: 1px !important; }
    .node.running rect { fill: #eff6ff !important; stroke: #3b82f6 !important; stroke-width: 3px !important; stroke-dasharray: 5, 5 !important; }
    .node.done rect { fill: #f0fdf4 !important; stroke: #22c55e !important; stroke-width: 2px !important; }
    .node.error rect { fill: #fef2f2 !important; stroke: #ef4444 !important; stroke-width: 2px !important; }
    .node.paused rect { fill: #fefce8 !important; stroke: #eab308 !important; stroke-width: 3px !important; }
  </style>
</head>
<body class="h-full flex flex-col overflow-hidden text-slate-800">

  <!-- Header -->
  <header class="bg-slate-900 text-white px-6 py-4 flex items-center justify-between shadow-md flex-none">
    <div class="flex items-center space-x-3">
      <div class="h-3 w-3 bg-emerald-500 rounded-full animate-ping" id="status-ping"></div>
      <h1 class="text-xl font-bold tracking-tight">iworkflow Live Dashboard</h1>
      <span class="text-xs bg-slate-800 text-slate-400 px-2.5 py-1 rounded-md font-mono" id="hdr-run-id">run: loading...</span>
    </div>
    <div class="flex items-center space-x-6">
      <div class="text-right">
        <div class="text-xs text-slate-400">Estado</div>
        <div class="text-sm font-semibold uppercase tracking-wider" id="hdr-status">Cargando...</div>
      </div>
      <div class="text-right">
        <div class="text-xs text-slate-400">Duración</div>
        <div class="text-sm font-semibold font-mono" id="hdr-duration">0s</div>
      </div>
      <div class="text-right">
        <div class="text-xs text-slate-400">Llamadas</div>
        <div class="text-sm font-semibold font-mono" id="hdr-calls">0</div>
      </div>
    </div>
  </header>

  <!-- Main Body -->
  <div class="flex flex-1 overflow-hidden relative">
    
    <!-- Split live view: graph as map, logs as console -->
    <main class="flex-1 min-h-0 overflow-hidden bg-slate-50 p-4">
      <div class="max-w-none mx-auto h-full min-w-0 grid grid-cols-1 lg:grid-cols-[minmax(320px,0.82fr)_minmax(0,1.18fr)] gap-4">
        <section class="min-w-0 min-h-0 bg-white rounded-2xl border border-slate-200 shadow-sm flex flex-col overflow-hidden">
          <div class="px-5 py-4 border-b border-slate-100 flex items-center justify-between gap-4">
            <div>
              <div class="text-xs uppercase tracking-wider text-slate-400 font-semibold">Mapa del workflow</div>
              <div class="text-sm text-slate-500">Click en un nodo para abrir su detalle.</div>
            </div>
            <div class="text-xs font-mono text-slate-400 shrink-0">graph TD</div>
          </div>
          <div class="flex-1 min-h-[320px] flex items-center justify-center p-4 bg-gradient-to-br from-white to-slate-50">
            <div class="mermaid w-full max-w-4xl p-6 flex justify-center" id="mermaid-container">
              <!-- Rendered SVG will land here -->
              <div class="text-slate-400 text-sm animate-pulse">Cargando grafo del workflow...</div>
            </div>
          </div>
        </section>

        <section class="min-w-0 min-h-0 bg-slate-950 border border-slate-800 rounded-2xl shadow-sm overflow-hidden flex flex-col">
          <div class="px-5 py-4 border-b border-slate-800 flex items-start justify-between gap-4">
            <div>
              <div class="text-xs uppercase tracking-wider text-slate-400 font-semibold" id="run-log-title">Actividad del run</div>
              <div class="text-xs text-slate-500 mt-1" id="run-log-subtitle">Salida reciente de agentes/comandos y eventos de estado.</div>
              <input id="log-search" type="search" placeholder="Buscar en logs, prompts y respuestas…" class="mt-3 w-full max-w-md rounded-lg border border-slate-800 bg-slate-900 px-3 py-2 text-xs text-slate-200 placeholder:text-slate-600 focus:border-cyan-500 focus:outline-none focus:ring-1 focus:ring-cyan-500" />
            </div>
            <div class="text-right shrink-0 space-y-2">
              <div>
                <div class="text-xs font-mono text-slate-500">events.jsonl</div>
                <div id="latest-output" class="font-mono text-xs text-emerald-300/80 truncate max-w-[300px] mt-1">Esperando eventos...</div>
              </div>
              <div id="selection-actions" class="hidden justify-end gap-2">
                <button onclick="openSelectedDetail()" class="rounded-md border border-slate-700 px-2 py-1 text-xs text-slate-300 hover:border-slate-500 hover:text-white">Detalle</button>
                <button onclick="clearStepSelection()" class="rounded-md border border-slate-700 px-2 py-1 text-xs text-slate-300 hover:border-slate-500 hover:text-white">Ver todo</button>
              </div>
            </div>
          </div>
          <div id="run-log" class="flex-1 min-h-[320px] overflow-y-auto overflow-x-hidden p-4 space-y-2 font-mono text-xs text-slate-300">
            <div class="text-slate-500 italic">Aún no hay eventos.</div>
          </div>
        </section>
      </div>
    </main>

    <!-- Side drawer for step details -->
    <aside id="drawer" class="fixed right-0 top-0 bottom-0 w-[500px] bg-white border-l border-slate-200 shadow-2xl z-50 flex flex-col transform translate-x-full transition-transform duration-300 ease-in-out">
      <!-- Drawer Header -->
      <div class="p-6 border-b border-slate-100 flex items-center justify-between bg-slate-50">
        <div>
          <h2 class="text-lg font-bold text-slate-900" id="dr-title">Detalles del Paso</h2>
          <span class="text-xs font-mono bg-slate-200 text-slate-600 px-2 py-0.5 rounded" id="dr-kind">kind</span>
        </div>
        <button onclick="closeDrawer()" class="text-slate-400 hover:text-slate-600 focus:outline-none p-1.5 hover:bg-slate-200 rounded-md transition-colors">
          <svg class="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
          </svg>
        </button>
      </div>

      <!-- Drawer Content -->
      <div class="flex-1 overflow-y-auto p-6 space-y-6">
        <!-- Status card -->
        <div class="bg-slate-50 p-4 rounded-lg border border-slate-100 flex items-center justify-between">
          <div>
            <div class="text-xs text-slate-400 uppercase font-semibold">Estado</div>
            <div class="text-sm font-bold uppercase mt-0.5" id="dr-status">PENDIENTE</div>
          </div>
          <div class="text-right">
            <div class="text-xs text-slate-400 uppercase font-semibold">Proveedor / Modelo</div>
            <div class="text-sm font-semibold mt-0.5" id="dr-provider">N/A</div>
          </div>
        </div>

        <!-- Details tabs -->
        <div class="border-b border-slate-200">
          <nav class="flex space-x-6" aria-label="Tabs">
            <button onclick="setTab('prompt')" id="tab-btn-prompt" class="border-b-2 border-blue-500 pb-3 text-sm font-medium text-blue-600 focus:outline-none">Prompt</button>
            <button onclick="setTab('result')" id="tab-btn-result" class="border-b-2 border-transparent pb-3 text-sm font-medium text-slate-500 hover:border-slate-300 hover:text-slate-700 focus:outline-none">Resultado</button>
            <button onclick="setTab('events')" id="tab-btn-events" class="border-b-2 border-transparent pb-3 text-sm font-medium text-slate-500 hover:border-slate-300 hover:text-slate-700 focus:outline-none">Eventos</button>
          </nav>
        </div>

        <!-- Tab content panes -->
        <div class="space-y-4">
          <!-- Prompt pane -->
          <div id="pane-prompt" class="tab-pane space-y-2">
            <div class="text-xs text-slate-400 font-semibold">Prompt renderizado enviado al agente:</div>
            <pre class="bg-slate-900 text-slate-200 p-4 rounded-lg overflow-x-auto text-xs whitespace-pre-wrap font-mono leading-relaxed" id="val-prompt">Ninguno</pre>
          </div>

          <!-- Result pane -->
          <div id="pane-result" class="tab-pane hidden space-y-2">
            <div class="text-xs text-slate-400 font-semibold">Resultado devuelto por el paso:</div>
            <pre class="bg-slate-900 text-slate-200 p-4 rounded-lg overflow-x-auto text-xs font-mono" id="val-result">{}</pre>
          </div>

          <!-- Events pane -->
          <div id="pane-events" class="tab-pane hidden space-y-2">
            <div class="text-xs text-slate-400 font-semibold">Historial de eventos de este paso:</div>
            <div class="space-y-2 max-h-96 overflow-y-auto" id="val-events">
              <!-- Events list -->
            </div>
          </div>
        </div>
      </div>
    </aside>
  </div>

  <script type="module">
    let activeTab = 'prompt';
    let selectedStepId = null;
    let logQuery = '';
    let baseMermaid = '';
    let currentData = { events: [], steps: {}, spec: null };

    function fnv1a32(text) {
      let h = 0x811c9dc5;
      const bytes = new TextEncoder().encode(String(text || 'node'));
      for (const b of bytes) {
        h ^= b;
        h = Math.imul(h, 0x01000193) >>> 0;
      }
      return h.toString(16).padStart(8, '0');
    }

    function mermaidNodeId(value) {
      const raw = String(value || 'node');
      if (/^[A-Za-z_][A-Za-z0-9_]*$/.test(raw)) return raw;
      let sanitized = raw.replace(/[^A-Za-z0-9_]/g, '_').replace(/^_+|_+$/g, '') || 'node';
      if (!/^[A-Za-z_]/.test(sanitized)) sanitized = `n_${sanitized}`;
      return `${sanitized}_${fnv1a32(raw)}`;
    }


    window.selectStep = function(stepId) {
      selectedStepId = stepId;
      const step = findStepInSpec(stepId);
      if (step && step.kind === 'command') {
        window.setTab('result');
      } else {
        window.setTab('prompt');
      }
      updateRunLog();
      updateDrawer();
    };

    window.openSelectedDetail = function() {
      if (!selectedStepId) return;
      document.getElementById('drawer').classList.remove('translate-x-full');
      updateDrawer();
    };

    window.clearStepSelection = function() {
      selectedStepId = null;
      document.getElementById('drawer').classList.add('translate-x-full');
      updateRunLog();
    };

    window.closeDrawer = function() {
      document.getElementById('drawer').classList.add('translate-x-full');
    };

    window.setTab = function(tab) {
      activeTab = tab;
      document.querySelectorAll('.tab-pane').forEach(el => el.classList.add('hidden'));
      document.getElementById(`pane-${tab}`).classList.remove('hidden');

      document.querySelectorAll('[id^="tab-btn-"]').forEach(el => {
        el.classList.remove('border-blue-500', 'text-blue-600');
        el.classList.add('border-transparent', 'text-slate-500');
      });
      const activeBtn = document.getElementById(`tab-btn-${tab}`);
      activeBtn.classList.remove('border-transparent', 'text-slate-500');
      activeBtn.classList.add('border-blue-500', 'text-blue-600');
    };

    function updateDrawer() {
      if (!selectedStepId) return;
      const step = findStepInSpec(selectedStepId);
      if (!step) return;

      document.getElementById('dr-title').innerText = selectedStepId;
      document.getElementById('dr-kind').innerText = step.kind;

      // Extract events for this step
      const stepEvents = currentData.events.filter(e => eventMatchesStep(e, selectedStepId));

      // Determine step status
      let status = 'PENDIENTE';
      let provider = 'N/A';
      
      const doneEv = stepEvents.find(e => e.event === 'done');
      const pausedEv = stepEvents.find(e => e.event === 'checkpoint_pending');
      const errorEv = stepEvents.find(e => e.event === 'error' || e.event === 'exhausted' || e.event === 'timeout');
      const dispatchEv = stepEvents.find(e => e.event === 'dispatch');

      if (doneEv) {
        status = 'COMPLETADO';
        provider = doneEv.provider || 'N/A';
      } else if (errorEv) {
        status = 'ERROR';
        provider = errorEv.provider || 'N/A';
      } else if (pausedEv) {
        status = 'PAUSADO';
        provider = 'human';
      } else if (dispatchEv) {
        status = 'EJECUTÁNDOSE';
        provider = dispatchEv.provider || 'N/A';
      }

      const statusEl = document.getElementById('dr-status');
      statusEl.innerText = status;
      statusEl.className = 'text-sm font-bold uppercase mt-0.5 ' + 
        (status === 'COMPLETADO' ? 'text-emerald-600' : 
         status === 'ERROR' ? 'text-red-600' : 
         status === 'PAUSADO' ? 'text-yellow-600' :
         status === 'EJECUTÁNDOSE' ? 'text-blue-600 animate-pulse' : 'text-slate-500');

      document.getElementById('dr-provider').innerText = provider;

      // Render tab values
      // 1. Prompt
      const promptEv = [...stepEvents].reverse().find(e => e.event === 'prompt' && e.text);
      document.getElementById('val-prompt').innerText = promptEv?.text || step.prompt || (step.agent ? step.agent.prompt : '') || 'N/A';

      // 2. Result + live output
      const resultObj = currentData.steps[selectedStepId];
      const outputText = stepEvents
        .filter(e => e.event === 'output' && e.text)
        .map(e => `[${e.stream || 'stdout'}] ${e.text}`)
        .join('');
      const resultText = resultObj ? JSON.stringify(resultObj, null, 2) : '{}';
      document.getElementById('val-result').innerText = outputText
        ? `${outputText}\n\n--- final result ---\n${resultText}`
        : resultText;

      // 3. Events list
      const evContainer = document.getElementById('val-events');
      evContainer.innerHTML = '';
      if (stepEvents.length === 0) {
        evContainer.innerHTML = '<div class="text-slate-400 text-xs italic">Ningún evento registrado.</div>';
      } else {
        stepEvents.forEach(ev => {
          const div = document.createElement('div');
          div.className = 'p-3 bg-slate-900 text-slate-300 rounded border border-slate-800 font-mono text-xs';
          const preview = ev.event === 'output' && ev.text
            ? {...ev, text: ev.text.length > 240 ? ev.text.slice(0, 240) + '…' : ev.text}
            : ev;
          div.innerText = `[${new Date(ev.ts * 1000).toLocaleTimeString()}] ${ev.event.toUpperCase()} - ${JSON.stringify(preview)}`;
          evContainer.appendChild(div);
        });
      }
    }

    function eventMatchesStep(ev, stepId) {
      if (!ev || !ev.label || !stepId) return false;
      const label = String(ev.label);
      if (label === stepId) return true;
      if (label.startsWith(stepId + '#')) return true;
      if (label.includes(':' + stepId)) return true;
      if (label.includes('/')) return label.split('/').pop() === stepId;
      return false;
    }

    function normalizedStepId(label) {
      return String(label || '').split('/').pop().split(':').pop().split('#')[0];
    }

    function findStepInSpec(stepId) {
      if (!currentData.spec) return null;
      // Search recursively in spec steps and loop bodies
      function search(steps) {
        for (const s of steps) {
          if (s.id === stepId) return s;
          if (s.body) {
            const found = search(s.body);
            if (found) return found;
          }
        }
        return null;
      }
      return search(currentData.spec.steps || []);
    }

    async function init() {
      // 1. Fetch config
      const cfg = await (await fetch('/api/config')).json();
      document.getElementById('hdr-run-id').innerText = `run: ${cfg.run_id}`;

      // 2. Fetch spec & mermaid
      const spec = await (await fetch('/api/spec')).json();
      currentData.spec = spec;

      const mermaidCode = await (await fetch('/api/mermaid')).text();
      baseMermaid = mermaidCode;

      // Render initial graph
      await updateUI();

      // Start polling
      setInterval(poll, 1500);
    }

    async function poll() {
      try {
        const eventsText = await (await fetch('/api/events')).text();
        const events = eventsText.trim().split('\\n').filter(Boolean).map(line => {
          try {
            return JSON.parse(line);
          } catch(e) {
            return null;
          }
        }).filter(Boolean);

        const steps = await (await fetch('/api/steps')).json();

        currentData.events = events;
        currentData.steps = steps;

        await updateUI();
        updateDrawer();
      } catch (e) {
        console.error("Poll failed:", e);
      }
    }

    async function updateUI() {
      if (!baseMermaid) return;

      // Calculate step states based on events
      const stepStates = {};
      currentData.events.forEach(e => {
        if (!e.label) return;
        // Strip iteration suffix (e.g. L#0/chat -> chat)
        let stepId = e.label;
        if (stepId.includes('/')) stepId = stepId.split('/').pop();
        if (stepId.includes(':')) stepId = stepId.split(':').pop();

        if (e.event === 'checkpoint_pending') {
          if (stepStates[stepId] !== 'done' && stepStates[stepId] !== 'error') stepStates[stepId] = 'paused';
        } else if (e.event === 'dispatch' || e.event === 'output' || e.event === 'heartbeat') {
          if (stepStates[stepId] !== 'done' && stepStates[stepId] !== 'error' && stepStates[stepId] !== 'paused') stepStates[stepId] = 'running';
        } else if (e.event === 'done') {
          stepStates[stepId] = 'done';
        } else if (e.event === 'error' || e.event === 'exhausted' || e.event === 'timeout') {
          stepStates[stepId] = 'error';
        }
      });

      // Build updated mermaid code
      let code = baseMermaid;
      code += '\\n\\nclassDef pending fill:#f8fafc,stroke:#cbd5e1,color:#475569';
      code += '\\nclassDef running fill:#eff6ff,stroke:#3b82f6,stroke-width:2.5px,color:#1d4ed8';
      code += '\\nclassDef done fill:#f0fdf4,stroke:#22c55e,color:#15803d';
      code += '\\nclassDef error fill:#fef2f2,stroke:#ef4444,color:#b91c1c';
      code += '\nclassDef paused fill:#fefce8,stroke:#eab308,stroke-width:2.5px,color:#854d0e';
      code += '\\nclassDef selected fill:#ecfeff,stroke:#06b6d4,stroke-width:3px,color:#0e7490\\n';

      // Set class for each node in spec
      function applyClasses(steps) {
        steps.forEach(s => {
          const state = stepStates[s.id] || 'pending';
          code += `\\nclass ${s.id} ${state}`;
          if (s.body) applyClasses(s.body);
        });
      }
      applyClasses(currentData.spec.steps || []);
      if (selectedStepId) code += `\nclass ${selectedStepId} selected`;

      // Render Mermaid graph
      const container = document.getElementById('mermaid-container');
      try {
        const { svg } = await window.mermaid.render('graph-div', code);
        container.innerHTML = svg;
        wireGraphClicks();
      } catch (err) {
        console.error("Mermaid render error:", err);
        container.innerHTML = `
          <div class="text-left max-w-full rounded-lg border border-red-200 bg-red-50 p-4 text-red-900">
            <div class="font-semibold">Mermaid render error</div>
            <pre class="mt-2 whitespace-pre-wrap text-xs">${String(err?.message || err)}</pre>
            <details class="mt-3">
              <summary class="cursor-pointer text-sm font-medium">Generated Mermaid source</summary>
              <pre class="mt-2 max-h-80 overflow-auto whitespace-pre text-xs">${escapeHtml(code)}</pre>
            </details>
          </div>`;
      }

      // Update Header stats
      // 1. Overall Status
      let status = 'RUNNING';
      const lastEv = currentData.events[currentData.events.length - 1];
      if (lastEv && lastEv.event === 'run') {
        status = lastEv.status === 'DONE' ? 'DONE' : lastEv.status === 'ERROR' ? 'ERROR' : 'RUNNING';
      } else {
        const states = Object.values(stepStates);
        if (states.includes('error')) status = 'ERROR';
        else if (states.includes('paused')) status = 'PAUSED';
        else if (states.length && currentData.spec.steps.every(s => stepStates[s.id] === 'done')) status = 'DONE';
        else if (states.includes('running')) status = 'RUNNING';
      }
      const statusEl = document.getElementById('hdr-status');
      statusEl.innerText = status;
      statusEl.className = 'text-sm font-semibold uppercase tracking-wider ' + 
        (status === 'DONE' ? 'text-emerald-400' :
         status === 'ERROR' ? 'text-red-400' :
         status === 'PAUSED' ? 'text-yellow-300' : 'text-blue-400 animate-pulse');

      // 2. Duration
      const firstEv = currentData.events[0];
      if (firstEv && lastEv) {
        const dur = Math.round(lastEv.ts - firstEv.ts);
        document.getElementById('hdr-duration').innerText = `${dur}s`;
      }

      // 3. Calls count
      const calls = currentData.events.filter(e => e.event === 'dispatch').length;
      document.getElementById('hdr-calls').innerText = calls;

      updateRunLog();
    }

    function flattenSteps(steps, out = []) {
      (steps || []).forEach(step => {
        out.push(step);
        if (step.body) flattenSteps(step.body, out);
      });
      return out;
    }

    function wireGraphClicks() {
      const svg = document.querySelector('#mermaid-container svg');
      if (!svg || !currentData.spec) return;
      const nodes = Array.from(svg.querySelectorAll('.node'));
      flattenSteps(currentData.spec.steps || []).forEach(step => {
        const node = nodes.find(n => n.textContent.trim().startsWith(`${step.id} `));
        if (!node) return;
        node.style.cursor = 'pointer';
        node.setAttribute('role', 'button');
        node.setAttribute('tabindex', '0');
        node.setAttribute('aria-label', `Filtrar logs de ${step.id}`);
        node.addEventListener('click', ev => {
          ev.preventDefault();
          ev.stopPropagation();
          window.selectStep(step.id);
        }, {capture: true});
        node.addEventListener('keydown', ev => {
          if (ev.key === 'Enter' || ev.key === ' ') {
            ev.preventDefault();
            window.selectStep(step.id);
          }
        });
      });
    }

    function providerLogo(provider) {
      const key = String(provider || '').toLowerCase();
      if (key === 'codex') return 'CX';
      if (key === 'gemini') return 'GM';
      if (key === 'claude') return 'CL';
      if (key === 'cursor') return 'CU';
      if (key === 'local') return '$';
      return key ? key.slice(0, 2).toUpperCase() : 'AI';
    }

    function providerModelForEvent(ev) {
      const label = ev.label;
      const related = [...currentData.events].reverse().find(e =>
        e.label === label && (e.event === 'done' || e.event === 'dispatch') && (e.provider || e.model)
      );
      const provider = ev.provider || related?.provider || 'unknown';
      const model = ev.model || related?.model || 'default';
      return {provider, model};
    }

    function compactCommand(command) {
      if (!command) return 'herramienta';
      const text = String(command).replace(/\\s+/g, ' ').trim();
      return text.length > 120 ? text.slice(0, 117) + '…' : text;
    }

    function formatCodexJson(obj) {
      if (!obj || typeof obj !== 'object') return null;
      if (obj.type === 'thread.started') return {kind: 'meta', text: 'Conversación LLM iniciada'};
      if (obj.type === 'turn.started') return {kind: 'meta', text: 'El LLM empezó a razonar'};
      if (obj.type === 'turn.completed') {
        const u = obj.usage || {};
        const parts = [];
        if (u.input_tokens !== undefined) parts.push(`${u.input_tokens} input`);
        if (u.cached_input_tokens !== undefined) parts.push(`${u.cached_input_tokens} cached`);
        if (u.output_tokens !== undefined) parts.push(`${u.output_tokens} output`);
        if (u.reasoning_output_tokens !== undefined) parts.push(`${u.reasoning_output_tokens} reasoning`);
        return {kind: 'meta', text: parts.length ? `Turno LLM completado · ${parts.join(' · ')}` : 'Turno LLM completado'};
      }
      if (obj.type === 'item.started') {
        const item = obj.item || {};
        if (item.type === 'command_execution') return {kind: 'tool', text: `Herramienta iniciada: ${compactCommand(item.command)}`};
        return {kind: 'meta', text: `${item.type || 'item'} iniciado`};
      }
      if (obj.type === 'item.completed') {
        const item = obj.item || {};
        if (item.type === 'agent_message') {
          return {kind: 'assistant', text: item.text || '(respuesta vacía)'};
        }
        if (item.type === 'command_execution') {
          const status = item.status ? ` · ${item.status}` : '';
          const exit = item.exit_code !== undefined && item.exit_code !== null ? ` · exit=${item.exit_code}` : '';
          return {kind: 'tool', text: `Herramienta completada: ${compactCommand(item.command)}${status}${exit}`};
        }
        return {kind: 'meta', text: `${item.type || 'item'} completado`};
      }
      if (obj.type) return {kind: 'meta', text: obj.type};
      return null;
    }

    function formatUnparsedOutputLine(line, ev) {
      const trimmed = String(line || '').trim();
      if (trimmed.startsWith('{') && trimmed.includes('"type"')) {
        if (trimmed.includes('"command_execution"') || trimmed.includes('"aggregated_output"')) {
          return {kind: 'tool', text: 'Herramienta: salida extensa compactada'};
        }
        if (trimmed.includes('"agent_message"')) {
          return {kind: 'assistant', text: 'Respuesta del LLM recibida en un fragmento JSON parcial. El próximo run la mostrará completa por el line-buffering nuevo.'};
        }
        return {kind: 'meta', text: 'Evento JSONL parcial compactado'};
      }
      const nonLocalProvider = ev.provider && ev.provider !== 'local';
      const looksLikeEscapedFragment = trimmed.includes('\\n') || trimmed.includes('\\"') || trimmed.length > 500;
      if (nonLocalProvider && looksLikeEscapedFragment) {
        return {kind: 'tool', text: 'Fragmento stdout del CLI compactado'};
      }
      return {kind: 'raw', text: `[${ev.stream || 'stdout'}] ${line}`};
    }

    function escapeHtml(value) {
      return String(value ?? '').replace(/[&<>"']/g, ch => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
      }[ch]));
    }

    function formatOutputEvent(ev) {
      const raw = String(ev.text || '');
      const lines = raw.split('\\n').filter(line => line.length > 0);
      if (lines.length === 0) return {kind: 'raw', text: `[${ev.stream || 'stdout'}]`};

      const formatted = [];
      let parsedAny = false;
      for (const line of lines) {
        try {
          const parsed = JSON.parse(line);
          const human = formatCodexJson(parsed);
          if (human) {
            parsedAny = true;
            formatted.push(human);
            continue;
          }
        } catch (_) {
          // Non-JSON provider output: keep it as process text.
        }
        formatted.push(formatUnparsedOutputLine(line, ev));
      }

      const compactedAny = formatted.some(part => part.kind !== 'raw');
      if (!parsedAny && !compactedAny) return {kind: 'raw', text: `[${ev.stream || 'stdout'}] ${raw.trimEnd()}`};
      const priority = formatted.find(part => part.kind === 'assistant')
        || formatted.find(part => part.kind === 'tool')
        || formatted[formatted.length - 1];
      return {
        kind: priority.kind,
        text: formatted.map(part => {
          if (part.kind === 'assistant') return `Respuesta del LLM:\\n\\n${part.text}`;
          if (part.kind === 'tool') return part.text;
          return part.text;
        }).join('\\n\\n'),
      };
    }

    function formatEventForLog(ev) {
      if (ev.event === 'output' && ev.text) return formatOutputEvent(ev);
      if (ev.event === 'prompt') return {kind: 'prompt', text: `Prompt enviado al LLM:\n\n${ev.text || ''}`};
      if (ev.event === 'dispatch') return {kind: 'meta', text: `started ${ev.provider || ''}`.trim()};
      if (ev.event === 'route') return {kind: 'meta', text: `route: ${ev.order || ev.provider || 'selected'}`};
      if (ev.event === 'done') {
        const tokens = ev.input_tokens !== undefined || ev.output_tokens !== undefined
          ? ` · tokens ${ev.input_tokens || 0} in / ${ev.output_tokens || 0} out`
          : '';
        return {kind: 'done', text: `done${ev.ms ? ` in ${ev.ms}ms` : ''}${ev.exit_code !== undefined ? ` exit=${ev.exit_code}` : ''}${tokens}`};
      }
      if (ev.event === 'checkpoint_pending') {
        const detail = ev.validation_error ? ` · ${ev.validation_error}` : '';
        return {kind: 'paused', text: `waiting for human input${detail}`};
      }
      if (ev.event === 'timeout') return {kind: 'error', text: `timeout${ev.timeout_s ? ` after ${ev.timeout_s}s` : ''}`};
      return {kind: 'meta', text: ev.event.toUpperCase()};
    }

    function summarizeEvent(ev) {
      const formatted = formatEventForLog(ev);
      return formatted.text.replace(/\\s+/g, ' ').trim().slice(0, 120);
    }

    function eventSearchText(ev) {
      const formatted = formatEventForLog(ev);
      const meta = [ev.label, ev.event, ev.provider, ev.model, ev.stream, formatted.kind].filter(Boolean).join(' ');
      return `${meta} ${formatted.text}`.toLowerCase();
    }

    function eventMatchesLogQuery(ev) {
      if (!logQuery) return true;
      return eventSearchText(ev).includes(logQuery);
    }

    function updateRunLog() {
      const log = document.getElementById('run-log');
      const latest = document.getElementById('latest-output');
      const title = document.getElementById('run-log-title');
      const subtitle = document.getElementById('run-log-subtitle');
      const actions = document.getElementById('selection-actions');
      if (!log || !latest || !title || !subtitle || !actions) return;

      log.innerHTML = '';
      const sourceEvents = selectedStepId
        ? currentData.events.filter(e => eventMatchesStep(e, selectedStepId))
        : currentData.events;
      const filteredEvents = sourceEvents.filter(eventMatchesLogQuery);
      const events = filteredEvents.slice(-120);

      title.innerText = selectedStepId ? `Actividad: ${selectedStepId}` : 'Actividad del run';
      subtitle.innerText = selectedStepId
        ? 'Mostrando solo la salida y eventos del paso seleccionado.'
        : 'Salida reciente de agentes/comandos y eventos de estado.';
      actions.classList.toggle('hidden', !selectedStepId);
      actions.classList.toggle('flex', !!selectedStepId);

      if (events.length === 0) {
        const empty = document.createElement('div');
        empty.className = 'text-slate-500 italic';
        empty.innerText = logQuery ? 'No hay eventos que coincidan con la búsqueda.' : 'Aún no hay eventos.';
        log.appendChild(empty);
        latest.innerText = logQuery ? `Sin resultados para: ${logQuery}` : selectedStepId ? `${selectedStepId}: sin eventos` : 'Esperando eventos...';
        return;
      }

      const lastOutput = [...events].reverse().find(e => e.event === 'output' && e.text);
      const lastEvent = events[events.length - 1];
      latest.innerText = lastOutput
        ? `${lastOutput.label}: ${summarizeEvent(lastOutput)}`
        : `${lastEvent.label || 'run'}: ${summarizeEvent(lastEvent)}`;

      events.forEach(ev => {
        const row = document.createElement('div');
        row.className = 'grid min-w-0 grid-cols-[92px_minmax(110px,150px)_minmax(0,1fr)] gap-3 items-start border-l-2 pl-3 py-1 ' +
          (ev.event === 'done' ? 'border-emerald-500 text-emerald-100' :
           ev.event === 'error' || ev.event === 'timeout' || ev.event === 'exhausted' ? 'border-red-500 text-red-100' :
           ev.event === 'checkpoint_pending' ? 'border-yellow-500 text-yellow-100' :
           ev.event === 'dispatch' ? 'border-blue-500 text-blue-100' : 'border-slate-700');

        const time = document.createElement('div');
        time.className = 'text-slate-500 whitespace-nowrap';
        time.innerText = new Date(ev.ts * 1000).toLocaleTimeString();

        const label = document.createElement('button');
        label.className = 'min-w-0 truncate text-left text-slate-300 hover:text-white underline-offset-2 hover:underline';
        label.innerText = ev.label || 'run';
        if (ev.label) label.onclick = () => window.selectStep(normalizedStepId(ev.label));

        const msg = document.createElement('div');
        const formatted = formatEventForLog(ev);
        msg.className = 'min-w-0 whitespace-pre-wrap break-words leading-relaxed';
        msg.style.overflowWrap = 'anywhere';
        if (formatted.kind === 'assistant') {
          msg.dataset.kind = 'assistant';
          msg.className += ' col-span-3 rounded-lg border border-emerald-900/60 bg-slate-900/80 p-3 text-slate-100';
          const meta = providerModelForEvent(ev);
          const badge = document.createElement('div');
          badge.className = 'mb-3 flex items-center gap-2 text-[11px] uppercase tracking-wide text-emerald-300';
          const logo = document.createElement('span');
          logo.className = 'rounded bg-emerald-400/10 px-1.5 py-0.5 font-bold';
          logo.innerText = providerLogo(meta.provider);
          const providerName = document.createElement('span');
          providerName.innerText = meta.provider;
          const modelName = document.createElement('span');
          modelName.className = 'text-slate-500';
          modelName.innerText = meta.model || 'default';
          badge.appendChild(logo);
          badge.appendChild(providerName);
          badge.appendChild(modelName);
          const body = document.createElement('div');
          body.className = 'whitespace-pre-wrap';
          body.innerText = formatted.text;
          msg.appendChild(badge);
          msg.appendChild(body);
        } else if (formatted.kind === 'prompt') {
          msg.dataset.kind = 'prompt';
          msg.className += ' col-span-3 rounded-lg border border-cyan-900/60 bg-cyan-950/40 p-3 text-cyan-50';
          msg.innerText = formatted.text;
        } else if (formatted.kind === 'tool') {
          msg.className += ' text-slate-400';
          msg.innerText = formatted.text;
        } else {
          msg.innerText = formatted.text;
        }

        row.appendChild(time);
        row.appendChild(label);
        row.appendChild(msg);
        log.appendChild(row);
      });
      const firstAssistant = selectedStepId ? log.querySelector('[data-kind="assistant"]') : null;
      if (firstAssistant) {
        log.scrollTop = Math.max(0, firstAssistant.offsetTop - log.offsetTop - 8);
      } else {
        log.scrollTop = log.scrollHeight;
      }
    }

    document.addEventListener('input', ev => {
      if (ev.target && ev.target.id === 'log-search') {
        logQuery = ev.target.value.toLowerCase().trim();
        updateRunLog();
      }
    });

    init();
  </script>
</body>
</html>
"""


class DashboardHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        # Suppress standard logging output to keep the console clean
        pass

    def do_GET(self):
        from urllib.parse import urlparse
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/config":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "run_id": self.server.run_id,
                "journal_dir": self.server.journal_dir,
            }).encode("utf-8"))
            return

        if path == "/api/spec":
            run_dir = Path(self.server.journal_dir) / "runs" / self.server.run_id
            spec_path = run_dir / "spec.json"
            if not spec_path.exists():
                self.send_error(404, "spec.json not found")
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(spec_path.read_bytes())
            return

        if path == "/api/events":
            run_dir = Path(self.server.journal_dir) / "runs" / self.server.run_id
            events_path = run_dir / "events.jsonl"
            if not events_path.exists():
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(events_path.read_bytes())
            return

        if path == "/api/steps":
            run_dir = Path(self.server.journal_dir) / "runs" / self.server.run_id
            steps_path = run_dir / "wf-steps.json"
            if not steps_path.exists():
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(steps_path.read_bytes())
            return

        if path == "/api/mermaid":
            run_dir = Path(self.server.journal_dir) / "runs" / self.server.run_id
            spec_path = run_dir / "spec.json"
            if not spec_path.exists():
                self.send_error(404, "spec.json not found")
                return
            try:
                spec = json.loads(spec_path.read_text(encoding="utf-8"))
                from iworkflow.graph import spec_to_mermaid
                mermaid_code = spec_to_mermaid(spec)
            except Exception as e:
                self.send_error(500, f"Error generating graph: {e}")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(mermaid_code.encode("utf-8"))
            return

        if path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_DASHBOARD.encode("utf-8"))
            return

        self.send_error(404, "File not found")


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    pass


def start_dashboard(run_id: str, journal_dir: str, port: int = 8000) -> None:
    # Set address reuse options
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer(("localhost", port), DashboardHandler)
    server.run_id = run_id
    server.journal_dir = journal_dir
    print(f"\niworkflow Live Dashboard started at http://localhost:{port}/")
    print("Press Ctrl+C to stop the dashboard server.")
    
    # Auto-open browser in background thread
    import threading
    import webbrowser
    def open_browser():
        time.sleep(1.0)
        try:
            webbrowser.open(f"http://localhost:{port}/")
        except Exception:
            pass
    threading.Thread(target=open_browser, daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down dashboard server...")
        server.server_close()
