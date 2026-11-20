// Shapes of the data the Python server sends. Keep in sync with app.py.

export type JobState = "queued" | "running" | "paused" | "done" | "error";
export type Stage = "get" | "find" | "render";
export type Target = "youtube" | "instagram";
export type Privacy = "private" | "unlisted" | "public";
export type Ratio = "9:16" | "4:5" | "1:1" | "16:9";

export interface UploadInfo {
  state: "queued" | "uploading" | "done" | "error";
  step?: string;
  url?: string;
  error?: string;
  privacy?: Privacy | null;
}

export interface Clip {
  file: string;
  title: string;
  description: string;
  hashtags: string[];
  start_s: number;
  end_s: number;
  from: string;
  to: string;
  seconds: number;
  score: number;
  ratio?: Ratio;
  uploads?: Partial<Record<Target, UploadInfo>>;
}

export interface JobOptions {
  clips: number;
  length: number;
  picker: "audio" | "gemini" | "claude";
  layout: "blur" | "crop";
  ratio: Ratio;
  model: string;
  lang: string | null;
  auto: Target[];
  yt_privacy: Privacy;
}

export interface Job {
  id: string;
  title: string;
  state: JobState;
  created: number;
  stage: Stage;
  percent: number | null;
  step: string | null;
  error: string | null;
  duration: number | null;
  options: JobOptions;
  live: boolean | null;
  pausing: boolean | null;
  clip_count: number;
  clips?: Clip[];
  log?: string[];
}

export interface Settings {
  youtube: { configured: boolean; connected: boolean };
  instagram: { connected: boolean; user_id: string; host: "facebook" | "instagram" };
  redirect_uri: string;
}

export interface Health {
  ffmpeg: boolean;
  gemini_key: boolean;
  claude_key: boolean;
}
