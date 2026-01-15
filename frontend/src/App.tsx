import React, { useMemo, useRef, useState } from "react";
import type { AgentEvent, Step } from "./types";

const API_BASE = ""; // proxied by Vite to backend

function formatTime(ts: number) {
  return new Date(ts).toLocaleTimeString();
}

export default function App() {
  const [userInput, setUserInput] = useState("Generate a short scene with a cat detective.");
  const [runId, setRunId] = useState<string | null>(null);
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [steps, setSteps] = useState<Record<string, Step>>({});
  const [uiRequest, setUiRequest] = useState<any | null>(null);

  const esRef = useRef<EventSource | null>(null);

  const orderedSteps = useMemo(() => {
    return Object.values(steps);
  }, [steps]);

  async function startRun() {
    setEvents([]);
    setSteps({});
    setUiRequest(null);

    const res = await fetch(`${API_BASE}/runs`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_input: userInput }),
    });
    const data = await res.json();
    const id = data.run_id as string;
    setRunId(id);

    // connect SSE
    const es = new EventSource(`${API_BASE}/runs/${id}/events`);
    esRef.current = es;

    es.addEventListener("message", (e: MessageEvent) => {
      const ev: AgentEvent = JSON.parse(e.data);
      setEvents((prev) => [...prev, ev]);

      // minimal reducer
      if (ev.type === "step.started") {
        const { stepId, title } = ev.payload;
        setSteps((prev) => ({
          ...prev,
          [stepId]: { stepId, title, status: "running" },
        }));
      }

      if (ev.type === "step.progress") {
        const { stepId, progress, message } = ev.payload;
        setSteps((prev) => {
          const cur = prev[stepId];
          if (!cur) return prev;
          return {
            ...prev,
            [stepId]: { ...cur, progress, message },
          };
        });
      }

      if (ev.type === "step.completed") {
        const { stepId, output } = ev.payload;
        setSteps((prev) => {
          const cur = prev[stepId];
          if (!cur) return prev;
          return {
            ...prev,
            [stepId]: { ...cur, status: "completed", progress: 1, output },
          };
        });
      }

      if (ev.type === "step.failed") {
        const { stepId } = ev.payload;
        setSteps((prev) => {
          const cur = prev[stepId];
          if (!cur) return prev;
          return {
            ...prev,
            [stepId]: { ...cur, status: "failed" },
          };
        });
      }

      if (ev.type === "ui.request") {
        setUiRequest(ev.payload);
      }

      if (ev.type === "run.completed" || ev.type === "run.failed" || ev.type === "run.canceled") {
        es.close();
      }
    });

    es.onerror = () => {
      // In production you’d implement retry/backoff here
      es.close();
    };
  }

  async function sendUIButton(buttonId: string) {
    if (!runId || !uiRequest) return;
    await fetch(`${API_BASE}/runs/${runId}/ui-response`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        requestId: uiRequest.requestId,
        buttonId,
      }),
    });
    setUiRequest(null);
  }

  return (
    <div style={{ fontFamily: "sans-serif", maxWidth: 980, margin: "24px auto", padding: 16 }}>
      <h2>LangGraph + SSE Agent Progress Demo</h2>

      <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
        <input
          style={{ flex: 1, padding: 10 }}
          value={userInput}
          onChange={(e) => setUserInput(e.target.value)}
        />
        <button style={{ padding: "10px 14px" }} onClick={startRun}>
          Start
        </button>
      </div>

      {runId && (
        <p style={{ marginTop: 8 }}>
          Run: <code>{runId}</code>
        </p>
      )}

      {uiRequest && (
        <div style={{ marginTop: 16, padding: 12, border: "1px solid #ddd", borderRadius: 8 }}>
          <div style={{ fontWeight: 600 }}>{uiRequest.ui?.title}</div>
          <div style={{ opacity: 0.8, marginTop: 6 }}>{uiRequest.ui?.description}</div>
          <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
            {(uiRequest.ui?.buttons ?? []).map((b: any) => (
              <button
                key={b.id}
                onClick={() => sendUIButton(b.id)}
                style={{ padding: "10px 14px" }}
              >
                {b.label}
              </button>
            ))}
          </div>
        </div>
      )}

      <h3 style={{ marginTop: 20 }}>Steps</h3>
      <div style={{ display: "grid", gap: 10 }}>
        {orderedSteps.map((s) => (
          <div key={s.stepId} style={{ border: "1px solid #eee", borderRadius: 8, padding: 12 }}>
            <div style={{ display: "flex", justifyContent: "space-between" }}>
              <div>
                <strong>{s.title}</strong> <code style={{ opacity: 0.7 }}>{s.stepId}</code>
              </div>
              <div>{s.status}</div>
            </div>

            {typeof s.progress === "number" && (
              <div style={{ marginTop: 10 }}>
                <div style={{ height: 8, background: "#eee", borderRadius: 999 }}>
                  <div
                    style={{
                      height: 8,
                      width: `${Math.round(s.progress * 100)}%`,
                      background: "#999",
                      borderRadius: 999,
                    }}
                  />
                </div>
                <div style={{ marginTop: 6, fontSize: 12, opacity: 0.8 }}>
                  {Math.round(s.progress * 100)}% {s.message ? `- ${s.message}` : ""}
                </div>
              </div>
            )}

            {s.output && (
              <pre style={{ marginTop: 10, background: "#fafafa", padding: 10, borderRadius: 6 }}>
{JSON.stringify(s.output, null, 2)}
              </pre>
            )}
          </div>
        ))}
      </div>

      <h3 style={{ marginTop: 20 }}>Event Log</h3>
      <div style={{ border: "1px solid #eee", borderRadius: 8, padding: 12, maxHeight: 320, overflow: "auto" }}>
        {events.map((e) => (
          <div key={e.seq} style={{ fontFamily: "monospace", fontSize: 12, marginBottom: 6 }}>
            <span style={{ opacity: 0.7 }}>[{formatTime(e.ts)}]</span>{" "}
            <strong>{e.type}</strong>{" "}
            <span style={{ opacity: 0.8 }}>{e.spanId ? `(${e.spanId})` : ""}</span>{" "}
            <span style={{ opacity: 0.85 }}>{JSON.stringify(e.payload)}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

