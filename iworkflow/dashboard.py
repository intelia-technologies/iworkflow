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
    
    <!-- Graph view -->
    <main class="flex-1 overflow-auto p-8 flex items-center justify-center bg-slate-50 relative">
      <div class="mermaid w-full max-w-4xl p-6 bg-white rounded-xl border border-slate-200 shadow-sm flex justify-center" id="mermaid-container">
        <!-- Rendered SVG will land here -->
        <div class="text-slate-400 text-sm animate-pulse">Cargando grafo del workflow...</div>
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
    let baseMermaid = '';
    let currentData = { events: [], steps: {}, spec: null };

    window.selectStep = function(stepId) {
      selectedStepId = stepId;
      const step = findStepInSpec(stepId);
      if (step && step.kind === 'command') {
        window.setTab('result');
      } else {
        window.setTab('prompt');
      }
      document.getElementById('drawer').classList.remove('translate-x-full');
      updateDrawer();
    };

    window.closeDrawer = function() {
      document.getElementById('drawer').classList.add('translate-x-full');
      selectedStepId = null;
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
      const stepEvents = currentData.events.filter(e => {
        if (e.label === selectedStepId) return true;
        // Match loop iterations or fanned out agents (e.g. L#0/chat or phase3_context:inspect_code)
        if (e.label && (e.label.startsWith(selectedStepId + '#') || e.label.includes(':' + selectedStepId))) return true;
        return false;
      });

      // Determine step status
      let status = 'PENDIENTE';
      let provider = 'N/A';
      
      const doneEv = stepEvents.find(e => e.event === 'done');
      const errorEv = stepEvents.find(e => e.event === 'error' || e.event === 'exhausted' || e.event === 'timeout');
      const dispatchEv = stepEvents.find(e => e.event === 'dispatch');

      if (doneEv) {
        status = 'COMPLETADO';
        provider = doneEv.provider || 'N/A';
      } else if (errorEv) {
        status = 'ERROR';
        provider = errorEv.provider || 'N/A';
      } else if (dispatchEv) {
        status = 'EJECUTÁNDOSE';
        provider = dispatchEv.provider || 'N/A';
      }

      const statusEl = document.getElementById('dr-status');
      statusEl.innerText = status;
      statusEl.className = 'text-sm font-bold uppercase mt-0.5 ' + 
        (status === 'COMPLETADO' ? 'text-emerald-600' : 
         status === 'ERROR' ? 'text-red-600' : 
         status === 'EJECUTÁNDOSE' ? 'text-blue-600 animate-pulse' : 'text-slate-500');

      document.getElementById('dr-provider').innerText = provider;

      // Render tab values
      // 1. Prompt
      const lastDispatch = [...stepEvents].reverse().find(e => e.event === 'dispatch' || e.event === 'route');
      document.getElementById('val-prompt').innerText = step.prompt || (step.agent ? step.agent.prompt : '') || 'N/A';

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

        if (e.event === 'dispatch' || e.event === 'output' || e.event === 'heartbeat') {
          if (stepStates[stepId] !== 'done' && stepStates[stepId] !== 'error') stepStates[stepId] = 'running';
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
      code += '\\nclassDef error fill:#fef2f2,stroke:#ef4444,color:#b91c1c\\n';

      // Set class for each node in spec
      function applyClasses(steps) {
        steps.forEach(s => {
          const state = stepStates[s.id] || 'pending';
          code += `\\nclass ${s.id} ${state}`;
          // Also set click event
          code += `\\nclick ${s.id} call selectStep()`;
          if (s.body) applyClasses(s.body);
        });
      }
      applyClasses(currentData.spec.steps || []);

      // Render Mermaid graph
      const container = document.getElementById('mermaid-container');
      try {
        const { svg } = await window.mermaid.render('graph-div', code);
        container.innerHTML = svg;
      } catch (err) {
        console.error("Mermaid render error:", err);
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
        else if (states.length && currentData.spec.steps.every(s => stepStates[s.id] === 'done')) status = 'DONE';
        else if (states.includes('running')) status = 'RUNNING';
      }
      const statusEl = document.getElementById('hdr-status');
      statusEl.innerText = status;
      statusEl.className = 'text-sm font-semibold uppercase tracking-wider ' + 
        (status === 'DONE' ? 'text-emerald-400' : status === 'ERROR' ? 'text-red-400' : 'text-blue-400 animate-pulse');

      // 2. Duration
      const firstEv = currentData.events[0];
      if (firstEv && lastEv) {
        const dur = Math.round(lastEv.ts - firstEv.ts);
        document.getElementById('hdr-duration').innerText = `${dur}s`;
      }

      // 3. Calls count
      const calls = currentData.events.filter(e => e.event === 'dispatch').length;
      document.getElementById('hdr-calls').innerText = calls;
    }

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
