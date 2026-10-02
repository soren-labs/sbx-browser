import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getToken, setToken } from "../api/http";
import { GITHUB_RETURN_KEY, handleGithubReturn } from "../api/github-return";

beforeEach(() => {
  setToken("REDACTED");
  history.replaceState(null, "", "/");
});
afterEach(() => {
  vi.restoreAllMocks();
  history.replaceState(null, "", "/");
});
const response = (status = 200, code?: string) =>
  new Response(JSON.stringify(code ? { error: { code } } : {}), { status });

describe("GitHub installation return", () => {
  it.each([
    "/?installation_id=12&state=REDACTED",
    "/?installation_id=12&state=REDACTED#/admin/github",
    "/?installation_id=12&state=REDACTED#/admin/github?broker=connected",
    "/#/admin/github?installation_id=12&state=REDACTED",
    "/#/integrations/github?installation_id=12&state=REDACTED",
  ])("submits the installation callback before adapting %s", async (url) => {
    history.replaceState(null, "", url);
    const fetch = vi.spyOn(globalThis, "fetch").mockImplementation(async () => {
      // The single-use state is scrubbed even while the request is pending.
      expect(location.href).not.toContain("REDACTED");
      expect(location.hash).toBe("");
      return response();
    });
    await handleGithubReturn();
    expect(fetch).toHaveBeenCalledOnce();
    expect(fetch.mock.calls[0][0]).toBe("/v1/github/app/authorize/callback");
    const init = fetch.mock.calls[0][1]!;
    expect(init).toMatchObject({
      method: "POST",
      credentials: "omit",
      headers: { Authorization: "Bearer REDACTED" },
    });
    expect(init.signal).toBeDefined();
    expect(JSON.parse(init.body as string)).toEqual({
      installation_id: 12,
      state: "REDACTED",
    });
    expect(location.pathname).toBe("/integrations");
    expect(location.search).toBe("?broker=returned");
    expect(localStorage.getItem(GITHUB_RETURN_KEY)).not.toContain("REDACTED");
    expect(getToken()).toBe("REDACTED");
  });

  it.each([
    "/#/admin/github?broker=connected",
    "/#/integrations/github?broker=connected",
  ])(
    "adapts broker success %s without replaying the server callback",
    async (url) => {
      history.replaceState(null, "", url);
      const fetch = vi.spyOn(globalThis, "fetch");
      await handleGithubReturn();
      expect(fetch).not.toHaveBeenCalled();
      expect(location.pathname + location.search).toBe(
        "/integrations?broker=returned",
      );
    },
  );

  it("does not invent success for a bare legacy route", async () => {
    history.replaceState(null, "", "/#/admin/github");
    await handleGithubReturn();
    expect(location.pathname + location.search).toBe("/integrations");
    expect(localStorage.getItem(GITHUB_RETURN_KEY)).toBeNull();
  });

  it("adapts a broker failure without leaking its query", async () => {
    history.replaceState(
      null,
      "",
      "/#/admin/github?broker_error=github_broker_claim",
    );
    await handleGithubReturn();
    expect(location.pathname + location.search).toBe(
      "/integrations?broker_error=authorization_failed",
    );
    expect(JSON.parse(localStorage.getItem(GITHUB_RETURN_KEY)!)).toMatchObject({
      error: "authorization_failed",
    });
  });

  it.each([
    "?installation_id=12",
    "?installation_id=abc&state=REDACTED",
    "?installation_id=0&state=REDACTED",
    "?installation_id=1e2&state=REDACTED",
    "?installation_id=9007199254740993&state=REDACTED",
    "?state=REDACTED",
  ])("rejects and scrubs a malformed callback %s", async (query) => {
    history.replaceState(null, "", "/" + query);
    const fetch = vi.spyOn(globalThis, "fetch");
    await handleGithubReturn();
    expect(fetch).not.toHaveBeenCalled();
    expect(location.search).toBe("?broker_error=authorization_failed");
  });

  it.each([
    [401, "unauthorized", "connection_key_required"],
    [403, "github_app_state", "authorization_expired"],
    [502, "github_broker_error", "authorization_failed"],
  ])(
    "reports callback HTTP %s without claiming connection",
    async (status, code, expected) => {
      history.replaceState(null, "", "/?installation_id=12&state=REDACTED");
      vi.spyOn(globalThis, "fetch").mockResolvedValue(response(status, code));
      await handleGithubReturn();
      expect(location.search).toBe(`?broker_error=${expected}`);
    },
  );

  it("reports missing workspace authentication without sending an empty bearer", async () => {
    setToken("");
    history.replaceState(null, "", "/?installation_id=12&state=REDACTED");
    const fetch = vi.spyOn(globalThis, "fetch");
    await handleGithubReturn();
    expect(fetch).not.toHaveBeenCalled();
    expect(location.search).toBe("?broker_error=connection_key_required");
  });

  it("reports a network failure and still scrubs callback credentials", async () => {
    history.replaceState(null, "", "/?installation_id=12&state=REDACTED");
    vi.spyOn(globalThis, "fetch").mockRejectedValue(
      new TypeError("Failed to fetch"),
    );
    await handleGithubReturn();
    expect(location.search).toBe("?broker_error=connection_failed");
  });
});
