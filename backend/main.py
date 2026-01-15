import asyncio
import json
import time
import uuid
from typing import Any, Dict, Optional, TypedDict

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from langgraph.graph import StateGraph, END

app = FastAPI()


# -----------------------------
# In-memory runtime store (demo only)
# -----------------------------
class RunStoreItem(TypedDict):
    queue: "asyncio.Queue[dict]"
    ui_waiter: "asyncio.Future[dict]"
    task: asyncio.Task


RUNS: Dict[str, RunStoreItem] = {}


def now_ms() -> int:
    return int(time.time() * 1000)


def ev(run_id: str, seq: int, type_: str, payload: dict, span_id: Optional[str] = None) -> dict:
    return {
        "v": 1,
        "runId": run_id,
        "seq": seq,
        "ts": now_ms(),
        "type": type_,
        "spanId": span_id,
        "payload": payload,
    }


async def push(q: asyncio.Queue, item: dict) -> None:
    await q.put(item)


# -----------------------------
# LangGraph state + nodes
# -----------------------------
class AgentState(TypedDict, total=False):
    user_input: str
    approved: bool
    result: str


def build_graph():
    graph = StateGraph(AgentState)

    async def step_plan(state: AgentState) -> AgentState:
        # just a placeholder; real code might call an LLM
        await asyncio.sleep(0.5)
        return state

    async def step_request_approval(state: AgentState) -> AgentState:
        # This node does NOT directly wait; we’ll manage waiting in the runner
        return state

    async def step_execute(state: AgentState) -> AgentState:
        await asyncio.sleep(0.5)
        user_input = state.get("user_input", "")
        state["result"] = f"✅ Done. You asked: {user_input}. Approved={state.get('approved', False)}"
        return state

    graph.add_node("plan", step_plan)
    graph.add_node("approval", step_request_approval)
    graph.add_node("execute", step_execute)

    graph.set_entry_point("plan")
    graph.add_edge("plan", "approval")
    graph.add_edge("approval", "execute")
    graph.add_edge("execute", END)

    return graph.compile()


GRAPH = build_graph()


# -----------------------------
# API models
# -----------------------------
class CreateRunRequest(BaseModel):
    user_input: str


class CreateRunResponse(BaseModel):
    run_id: str


class UIResponse(BaseModel):
    requestId: str
    buttonId: Optional[str] = None
    formData: Optional[Dict[str, Any]] = None
    selected: Optional[list[str]] = None


# -----------------------------
# Runner: executes graph and emits SSE events
# -----------------------------
async def run_agent(run_id: str, user_input: str, q: asyncio.Queue, ui_waiter: asyncio.Future):
    seq = 0

    async def emit(type_: str, payload: dict, span_id: Optional[str] = None):
        nonlocal seq
        seq += 1
        await push(q, ev(run_id, seq, type_, payload, span_id=span_id))

    await emit("run.started", {"name": "LangGraph SSE Demo", "input": {"user_input": user_input}})

    state: AgentState = {"user_input": user_input}

    # STEP 1: plan
    await emit("step.started", {"stepId": "plan", "title": "Plan", "kind": "planning"}, span_id="plan")
    await emit("log.appended", {"level": "info", "message": "Planning..."}, span_id="plan")

    state = await GRAPH.nodes["plan"].ainvoke(state)  # invoke node function
    await emit("step.completed", {"stepId": "plan"}, span_id="plan")

    # STEP 2: approval (human-in-the-loop)
    await emit("step.started", {"stepId": "approval", "title": "Approval", "kind": "custom"}, span_id="approval")
    request_id = f"rq_{uuid.uuid4().hex[:8]}"

    # Tell UI to show buttons and block
    await emit(
        "ui.request",
        {
            "requestId": request_id,
            "ui": {
                "kind": "buttons",
                "title": "Proceed with execution?",
                "description": "Click Approve to continue or Cancel to stop.",
                "buttons": [
                    {"id": "approve", "label": "Approve", "style": "primary"},
                    {"id": "cancel", "label": "Cancel", "style": "danger", "confirm": {"title": "Cancel run?"}},
                ],
            },
            "blocking": True,
            "timeoutMs": 60_000,
        },
        span_id="approval",
    )

    await emit("log.appended", {"level": "info", "message": "Waiting for user action..."}, span_id="approval")

    try:
        ui_payload = await asyncio.wait_for(ui_waiter, timeout=60.0)
    except asyncio.TimeoutError:
        await emit("step.failed", {"stepId": "approval", "error": {"message": "UI response timeout"}}, span_id="approval")
        await emit("run.failed", {"error": {"message": "UI response timeout", "retriable": False}})
        return

    # reset waiter for potential future interactions (not used in this minimal demo)
    # NOTE: We will replace it in store when receiving response. Here just proceed.
    button = ui_payload.get("buttonId")

    if button == "cancel":
        await emit("step.completed", {"stepId": "approval", "output": {"approved": False}}, span_id="approval")
        await emit("run.canceled", {"reason": "User canceled"})
        return

    state["approved"] = (button == "approve")
    await emit("step.completed", {"stepId": "approval", "output": {"approved": state["approved"]}}, span_id="approval")

    # STEP 3: execute
    await emit("step.started", {"stepId": "execute", "title": "Execute", "kind": "generation"}, span_id="execute")
    await emit("step.progress", {"stepId": "execute", "progress": 0.2, "message": "Starting..."}, span_id="execute")
    await asyncio.sleep(0.3)
    await emit("step.progress", {"stepId": "execute", "progress": 0.6, "message": "Working..."}, span_id="execute")
    state = await GRAPH.nodes["execute"].ainvoke(state)
    await emit("step.progress", {"stepId": "execute", "progress": 1.0, "message": "Done."}, span_id="execute")

    await emit("step.completed", {"stepId": "execute", "output": {"result": state.get("result")}}, span_id="execute")
    await emit("run.completed", {"output": state, "summary": state.get("result", "")})

    # signal SSE to end
    await push(q, {"_end": True})


# -----------------------------
# Routes
# -----------------------------
@app.post("/runs", response_model=CreateRunResponse)
async def create_run(req: CreateRunRequest):
    run_id = f"run_{uuid.uuid4().hex[:10]}"
    q: asyncio.Queue[dict] = asyncio.Queue()

    loop = asyncio.get_running_loop()
    ui_waiter: asyncio.Future[dict] = loop.create_future()

    task = asyncio.create_task(run_agent(run_id, req.user_input, q, ui_waiter))

    RUNS[run_id] = {"queue": q, "ui_waiter": ui_waiter, "task": task}
    return CreateRunResponse(run_id=run_id)


@app.get("/runs/{run_id}/events")
async def sse_events(run_id: str):
    if run_id not in RUNS:
        raise HTTPException(status_code=404, detail="run not found")

    q = RUNS[run_id]["queue"]

    async def event_stream():
        # Initial comment to open stream
        yield b": connected\n\n"
        while True:
            item = await q.get()
            if "_end" in item:
                yield b": end\n\n"
                break

            data = json.dumps(item, ensure_ascii=False)
            # SSE format: event + data
            # We keep "event: message" and put type inside payload.
            yield f"event: message\ndata: {data}\n\n".encode("utf-8")

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/runs/{run_id}/ui-response")
async def ui_response(run_id: str, resp: UIResponse):
    if run_id not in RUNS:
        raise HTTPException(status_code=404, detail="run not found")

    store = RUNS[run_id]
    fut = store["ui_waiter"]

    if fut.done():
        # already responded (or timed out). In a real system you’d store pending requests list.
        raise HTTPException(status_code=409, detail="no pending ui request")

    fut.set_result(resp.model_dump())

    # prepare next waiter for future ui.request (not used in this minimal demo)
    loop = asyncio.get_running_loop()
    store["ui_waiter"] = loop.create_future()

    return {"ok": True}


@app.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str):
    if run_id not in RUNS:
        raise HTTPException(status_code=404, detail="run not found")

    task = RUNS[run_id]["task"]
    task.cancel()
    return {"ok": True}

