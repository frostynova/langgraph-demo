export type AgentEvent = {
  v: 1;
  runId: string;
  seq: number;
  ts: number;
  type: string;
  spanId?: string;
  payload: any;
};

export type Step = {
  stepId: string;
  title: string;
  status: "idle" | "running" | "completed" | "failed";
  progress?: number;
  message?: string;
  output?: any;
};

