import { afterEach, expect, it, vi } from "vitest";
import { connections, connectionTerminal } from "../prototype/domain";
import { HttpSessionApi, setToken } from "../api/http";
afterEach(() => vi.restoreAllMocks());
it("does not treat a failed credential probe with HTTP 200 as successful", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(
      JSON.stringify({ status: "invalid", last_error: "auth_invalid" }),
      { status: 200 },
    ),
  );
  await expect(connections.check("spare", "verify")).rejects.toThrow(
    "auth_invalid",
  );
});
it("uses the default installation flow, with bridge credential distinct from installation", async () => {
  const fetch = vi
    .spyOn(globalThis, "fetch")
    .mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          authorize_url: "https://github.com/apps/sbx/installations/new",
        }),
        { status: 201 },
      ),
    );
  await new HttpSessionApi().beginGithubAuthorize();
  expect(fetch.mock.calls[0][0]).toContain("/github/install");
});
it("cleans up a removed account without parsing a 204 as JSON", async () => {
  const fetch = vi
    .spyOn(globalThis, "fetch")
    .mockResolvedValue(new Response(null, { status: 204 }));
  await connections.remove("spare");
  expect(fetch.mock.calls[0][1]?.method).toBe("DELETE");
});
it("stops auth polling on every real terminal state", () => {
  for (const s of [
    "verified",
    "materialized",
    "failed",
    "cancelled",
    "expired",
  ])
    expect(connectionTerminal(s)).toBe(true);
  expect(connectionTerminal("authenticating")).toBe(false);
});
it("consumes callback state without retaining it in browser history", async()=>{
 setToken("REDACTED");
 const {handleGithubReturn}=await import("../api/github-return");history.replaceState(null,"","/?installation_id=12&state=REDACTED");
 const fetch=vi.spyOn(globalThis,"fetch").mockResolvedValue(new Response("{}",{status:200}));await handleGithubReturn();expect(location.href).not.toContain("REDACTED");expect(location.pathname).toBe("/integrations");expect(JSON.parse(fetch.mock.calls[0][1]!.body as string)).toEqual({installation_id:12,state:"REDACTED"});history.replaceState(null,"","/");
});
