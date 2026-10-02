import type {
  DeliverInput,
  ErrorKind,
  IntegrationStatus,
  ModelInfo,
  NewSessionInput,
  ProviderInfo,
  Session,
  SessionChange,
  SessionChangesDiff,
  SessionDeliverResult,
  SessionFileDiff,
  SessionPhase,
  ActivityItem,
  Turn,
} from "./types";

/** API error carrying a product-level kind for actionable UX. */
export class ApiError extends Error {
  readonly kind: ErrorKind;
  readonly httpStatus: number;
  readonly subcode: string;
  readonly retryable: boolean;
  readonly retryAfter?: number;

  constructor(
    kind: ErrorKind,
    message: string,
    opts: {
      httpStatus?: number;
      subcode?: string;
      retryable?: boolean;
      retryAfter?: number;
    } = {},
  ) {
    super(message);
    this.name = "ApiError";
    this.kind = kind;
    this.httpStatus = opts.httpStatus ?? 0;
    this.subcode = opts.subcode ?? "internal";
    this.retryable = opts.retryable ?? false;
    this.retryAfter = opts.retryAfter;
  }
}

export function isApiError(e: unknown): e is ApiError {
  return e instanceof ApiError;
}

/** Live event surface for one session (SSE or fixture emitter). */
export interface SessionEventHandlers {
  historyAfterTurn?: number;
  onOpen?: () => void;
  onPhase?: (phase: SessionPhase) => void;
  onActivity?: (item: ActivityItem) => void;
  onActivities?: (items: ActivityItem[]) => void;
  onTurn?: (turn: Turn) => void;
  onSession?: (session: Session) => void;
  /** session.meta frame — effective provider/model once resolved. */
  onMeta?: (meta: { provider?: string | null; model?: string | null }) => void;
  onError?: (error: ApiError) => void;
  /** Fired when a dropped stream is re-established. */
  onReconnect?: () => void;
  /** Fired when the stream is down and a retry is pending. */
  onDisconnect?: (nextRetryMs: number) => void;
}

export interface SessionApi {
  listSessions(): Promise<Session[]>;
  getSession(id: string): Promise<Session>;
  getHistory?(id: string, before: number): Promise<{ turns: Turn[]; hasMore: boolean }>;
  readSessionCache?(id: string): Promise<Session | null>;
  writeSessionCache?(session: Session): Promise<void>;
  /**
   * Create a session. Resolves once the session shell exists (queued or
   * starting) — the UI navigates to it optimistically and follows live
   * events via subscribe().
   */
  createSession(input: NewSessionInput): Promise<Session>;
  /**
   * Submit a follow-up turn (POST /v2/sessions/{id}/messages). Returns the
   * refreshed session plus the queued turn number ``n`` when the message
   * was accepted (null when it was only queued on the session record).
   */
  sendFollowUp(
    sessionId: string,
    text: string,
  ): Promise<{ session: Session; n: number | null }>;
  /** Cancel outstanding work (queued turns drop, running turn stops). */
  stopSession(sessionId: string): Promise<Session>;
  /** Retry the failed step — a failed delivery re-publishes, a terminal
   * run verdict re-runs the (optionally overridden) prompt. */
  retrySession(sessionId: string, prompt?: string): Promise<Session>;
  /**
   * Deliver the session's materialized changes as a pull request
   * (POST .../deliver). Synchronous — resolves with the refreshed session
   * and the delivered revision (its delivery record carries the PR).
   * Safe to repeat: delivering again updates the same work branch/PR,
   * never reruns the provider task.
   */
  deliverSession(
    sessionId: string,
    input?: DeliverInput,
  ): Promise<SessionDeliverResult>;
  listProviders(): Promise<ProviderInfo[]>;
  listModels(): Promise<ModelInfo[]>;
  getIntegrations(): Promise<IntegrationStatus>;
  /** Read GitHub independently so provider failures cannot hide its status. */
  getGithubStatus(signal?: AbortSignal): Promise<IntegrationStatus["github"]>;
  /** Step 1 of Connect GitHub — returns the install URL to open. */
  beginGithubAuthorize(signal?: AbortSignal): Promise<{ url: string }>;
  listChanges(sessionId: string): Promise<SessionChange[]>;
  /**
   * File-level view of the latest ready revision (GET .../changes/diff):
   * file list + per-file stats — compact, no diff bodies.
   */
  listChangesDiff(sessionId: string): Promise<SessionChangesDiff>;
  /** Lazy per-file diff text (GET .../changes/diff?path=). */
  getFileDiff(sessionId: string, path: string, n?: number): Promise<SessionFileDiff>;
  /** Subscribe to the session-scoped event stream. Returns an unsubscribe. */
  subscribe(sessionId: string, handlers: SessionEventHandlers): () => void;
}
